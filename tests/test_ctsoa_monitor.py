from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from src.core.jobs.ctsoa.monitor import (
    ConfigurationError,
    load_config,
    parse_pending_payload,
    rsa_encrypt,
)


class CtsOaMonitorTests(unittest.TestCase):
    def test_rsa_encrypt_matches_site_payload_shape(self) -> None:
        key = RSA.generate(2048)
        flag = "-rsa"
        encrypted = rsa_encrypt(
            "secret",
            key.public_key().export_key().decode("ascii"),
            "nonce",
            flag,
        )

        self.assertTrue(encrypted.endswith(flag))
        ciphertext = base64.b64decode(encrypted[: -len(flag)])
        plaintext = PKCS1_v1_5.new(key).decrypt(ciphertext, b"invalid")
        self.assertEqual(plaintext, b"secretnonce")

    def test_parse_pending_payload_normalizes_rows_and_total(self) -> None:
        result = parse_pending_payload(
            {
                "datas": [
                    {
                        "requestid": "100",
                        "requestname": "<b>采购审批</b>",
                        "workflowname": "采购流程",
                        "creatorname": "张三",
                        "receivedate": "2026-07-30",
                        "receivetime": "09:15",
                        "statusname": "待处理",
                    }
                ]
            },
            {"totalcount": {"全部": "12", "超时": 2}},
        )

        self.assertEqual(result["pending_count"], 12)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["items"][0]["title"], "采购审批")
        self.assertEqual(result["items"][0]["received_at"], "2026-07-30 09:15")

    def test_parse_pending_payload_falls_back_to_returned_count(self) -> None:
        result = parse_pending_payload({"datas": [{}, {}]}, None, max_items=1)
        self.assertEqual(result["pending_count"], 2)
        self.assertEqual(result["returned_count"], 1)

    def test_config_requires_https(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "oa": {"base_url": "http://oa.example.com"},
                        "pending": {},
                        "output": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
