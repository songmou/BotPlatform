#!/usr/bin/env python3
"""Copy integration bindings between tenants owned by the same channel identity.

Re-binding a channel mints a brand new tenant (``organization-channel:`` bot
ids), which strands integration credentials on the previous tenant. Scheduled
scripts then fail with "尚未配置 <integration>" even though the member did
configure it.

This script finds tenants that share one exact channel identity
(platform + account_id + external_user_id) and copies missing integration rows
between them. The ciphertext store is never touched: ``metadata_json`` already
carries ``keychain_service``/``keychain_account``, and
``IntegrationService._stored_reference`` prefers those over deriving a
reference from the tenant id. Source rows are kept so the legacy route keeps
working.

Usage:
    python3 scripts/migrate_channel_integrations.py            # dry run
    python3 scripts/migrate_channel_integrations.py --apply    # write
    python3 scripts/migrate_channel_integrations.py --apply \
        --source <tenant> --target <tenant>   # restrict to one pair
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "data" / "system" / "botplatform.sqlite3"

IdentityKey = Tuple[str, str, str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit("找不到数据库文件：{}".format(path))
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _identity_groups(connection: sqlite3.Connection) -> Dict[IdentityKey, List[str]]:
    """Group tenants by the exact channel identity they answer for."""
    groups: Dict[IdentityKey, List[str]] = defaultdict(list)
    rows = connection.execute(
        "SELECT tenant_id, platform, account_id, external_user_id "
        "FROM channel_identities ORDER BY created_at"
    ).fetchall()
    for row in rows:
        key = (
            str(row["platform"] or ""),
            str(row["account_id"] or ""),
            str(row["external_user_id"] or ""),
        )
        # A blank component would over-match unrelated tenants.
        if not all(key):
            continue
        tenant_id = str(row["tenant_id"])
        if tenant_id not in groups[key]:
            groups[key].append(tenant_id)
    return {key: tenants for key, tenants in groups.items() if len(tenants) > 1}


def _integrations(
    connection: sqlite3.Connection, tenant_ids: List[str]
) -> Dict[str, Dict[str, sqlite3.Row]]:
    if not tenant_ids:
        return {}
    placeholders = ",".join("?" for _ in tenant_ids)
    rows = connection.execute(
        "SELECT tenant_id, integration_id, metadata_json, updated_at "
        "FROM integrations WHERE tenant_id IN ({})".format(placeholders),
        tuple(tenant_ids),
    ).fetchall()
    owned: Dict[str, Dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in rows:
        owned[str(row["tenant_id"])][str(row["integration_id"])] = row
    return owned


def _tenant_labels(
    connection: sqlite3.Connection, tenant_ids: List[str]
) -> Dict[str, str]:
    if not tenant_ids:
        return {}
    placeholders = ",".join("?" for _ in tenant_ids)
    rows = connection.execute(
        "SELECT tenant_id, bot_id FROM tenants WHERE tenant_id IN ({})".format(
            placeholders
        ),
        tuple(tenant_ids),
    ).fetchall()
    return {str(row["tenant_id"]): str(row["bot_id"]) for row in rows}


def _plan_copies(
    connection: sqlite3.Connection,
    source_filter: Optional[str],
    target_filter: Optional[str],
) -> List[Dict[str, str]]:
    """Return every (source -> target) integration copy worth performing."""
    planned: List[Dict[str, str]] = []
    for key, tenant_ids in sorted(_identity_groups(connection).items()):
        owned = _integrations(connection, tenant_ids)
        labels = _tenant_labels(connection, tenant_ids)
        available: Dict[str, str] = {}
        for tenant_id in tenant_ids:
            for integration_id in owned.get(tenant_id, {}):
                available.setdefault(integration_id, tenant_id)
        for target in tenant_ids:
            for integration_id, source in available.items():
                if source == target:
                    continue
                if integration_id in owned.get(target, {}):
                    continue
                if source_filter and source != source_filter:
                    continue
                if target_filter and target != target_filter:
                    continue
                planned.append(
                    {
                        "identity": "{}|{}|{}".format(*key),
                        "integration_id": integration_id,
                        "source": source,
                        "source_bot": labels.get(source, "?"),
                        "target": target,
                        "target_bot": labels.get(target, "?"),
                        "metadata_json": str(
                            owned[source][integration_id]["metadata_json"]
                        ),
                    }
                )
    return planned


def _apply(connection: sqlite3.Connection, planned: List[Dict[str, str]]) -> int:
    written = 0
    with connection:
        for item in planned:
            try:
                metadata = json.loads(item["metadata_json"])
            except ValueError:
                print(
                    "  跳过（元数据无法解析）：{} -> {}".format(
                        item["integration_id"], item["target"]
                    )
                )
                continue
            if not isinstance(metadata, dict):
                print(
                    "  跳过（元数据格式无效）：{} -> {}".format(
                        item["integration_id"], item["target"]
                    )
                )
                continue
            # Keep keychain_service untouched so both tenants read one secret.
            metadata["migrated_from"] = item["source"]
            metadata["migrated_at"] = _now()
            cursor = connection.execute(
                "INSERT INTO integrations(tenant_id, integration_id, "
                "metadata_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, integration_id) DO NOTHING",
                (
                    item["target"],
                    item["integration_id"],
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )
            written += cursor.rowcount if cursor.rowcount > 0 else 0
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="在共享同一渠道身份的租户之间补齐集成凭据绑定"
    )
    parser.add_argument(
        "--apply", action="store_true", help="真正写入；缺省仅做 dry-run 预览"
    )
    parser.add_argument("--source", default="", help="仅迁移来自该租户的绑定")
    parser.add_argument("--target", default="", help="仅迁移到该租户的绑定")
    parser.add_argument(
        "--database", default=str(DATABASE_PATH), help="数据库路径（默认平台主库）"
    )
    args = parser.parse_args(argv)

    connection = _connect(Path(args.database))
    try:
        planned = _plan_copies(
            connection, args.source.strip() or None, args.target.strip() or None
        )
        if not planned:
            print("没有需要补齐的集成绑定。")
            return 0

        print("待补齐的集成绑定共 {} 条：".format(len(planned)))
        for item in planned:
            print(
                "  [{}] {} : {}（{}）-> {}（{}）".format(
                    item["identity"],
                    item["integration_id"],
                    item["source"][:8],
                    item["source_bot"],
                    item["target"][:8],
                    item["target_bot"],
                )
            )

        if not args.apply:
            print("\n当前为 dry-run，未写入任何数据。确认无误后追加 --apply 执行。")
            return 0

        written = _apply(connection, planned)
        print("\n已写入 {} 条集成绑定。密文文件未做任何改动。".format(written))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
