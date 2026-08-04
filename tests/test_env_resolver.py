"""Unit tests for the layered environment variable resolver.

No web stack involved: an in-memory fake settings store stands in for the
real ``SettingsStore`` so resolution semantics can be checked in isolation.
"""

from __future__ import annotations

import unittest

from src.core.services.env_resolver import (
    EnvNameError,
    EnvResolver,
    mask,
    normalize_allowlist,
    validate_env_name,
)


class _FakeSettings:
    """Minimal stand-in for SettingsStore.env()."""

    def __init__(self, data: dict):
        self._data = data

    def env(self, tenant_id: str) -> dict:
        return dict(self._data.get(tenant_id, {}))


def _resolver(org_data: dict, global_data: dict) -> EnvResolver:
    return EnvResolver(_FakeSettings(org_data), lambda: dict(global_data))


class ValidateNameTest(unittest.TestCase):
    def test_valid_names_pass(self):
        for name in ("FOO", "FOO_BAR", "A1", "_PRIVATE"):
            self.assertEqual(validate_env_name(name), name)

    def test_lowercase_rejected(self):
        with self.assertRaises(EnvNameError):
            validate_env_name("foo")
        with self.assertRaises(EnvNameError):
            validate_env_name("Foo_Bar")

    def test_leading_digit_rejected(self):
        with self.assertRaises(EnvNameError):
            validate_env_name("1FOO")

    def test_reserved_name_rejected(self):
        for name in ("PATH", "HOME", "LD_PRELOAD", "PYTHONPATH", "ILINKBOT_X"):
            with self.assertRaises(EnvNameError):
                validate_env_name(name)

    def test_non_string_rejected(self):
        with self.assertRaises(EnvNameError):
            validate_env_name(123)


class NormalizeAllowlistTest(unittest.TestCase):
    def test_strips_and_dedupes(self):
        self.assertEqual(
            normalize_allowlist([" FOO ", "FOO", "BAR"]),
            ["FOO", "BAR"],
        )

    def test_reserved_filtered_out_raises(self):
        # A reserved name inside an allowlist is invalid -> rejected outright.
        with self.assertRaises(EnvNameError):
            normalize_allowlist(["FOO", "PATH"])

    def test_too_many_rejected(self):
        with self.assertRaises(EnvNameError):
            normalize_allowlist(["VAR_{}".format(i) for i in range(40)])

    def test_empty_is_empty(self):
        self.assertEqual(normalize_allowlist([]), [])
        self.assertEqual(normalize_allowlist(None), [])


class MaskTest(unittest.TestCase):
    def test_short_value_is_fully_masked(self):
        self.assertEqual(mask(""), "****")
        self.assertEqual(mask("short"), "****")
        self.assertEqual(mask("12345678"), "****")

    def test_long_value_keeps_ends(self):
        self.assertEqual(mask("super-secret-token"), "su****en")
        self.assertEqual(mask("abcdefghij"), "ab****ij")


class ResolveTest(unittest.TestCase):
    def test_org_overrides_global(self):
        resolver = _resolver(
            org_data={"t1": {"API_TOKEN": "org-value"}},
            global_data={"API_TOKEN": "global-value", "FEATURE": "on"},
        )
        result = resolver.resolve("t1", ["API_TOKEN", "FEATURE"])
        self.assertEqual(result["API_TOKEN"], "org-value")
        self.assertEqual(result["FEATURE"], "on")

    def test_global_used_when_no_org(self):
        resolver = _resolver(
            org_data={}, global_data={"API_TOKEN": "global-value"}
        )
        self.assertEqual(resolver.resolve("t1", ["API_TOKEN"])["API_TOKEN"], "global-value")

    def test_reserved_names_never_returned(self):
        resolver = _resolver(
            org_data={"t1": {"PATH": "/evil"}},
            global_data={"PATH": "/also-evil"},
        )
        self.assertNotIn("PATH", resolver.resolve("t1", ["PATH"]))

    def test_missing_name_omitted(self):
        resolver = _resolver(org_data={}, global_data={})
        self.assertEqual(resolver.resolve("t1", ["NOPE"]), {})


class DescribeTest(unittest.TestCase):
    def test_describe_sources(self):
        resolver = _resolver(
            org_data={"t1": {"API_TOKEN": "org-secret-value-1234"}},
            global_data={"API_TOKEN": "global-secret-value-1234", "ORPHAN": "x"},
        )
        rows = resolver.describe("t1", ["API_TOKEN", "ORPHAN", "MISSING"])
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["API_TOKEN"]["source"], "tenant")
        self.assertTrue(by_name["API_TOKEN"]["defined"])
        self.assertEqual(by_name["API_TOKEN"]["masked"], "or****34")
        self.assertEqual(by_name["ORPHAN"]["source"], "global")
        self.assertEqual(by_name["MISSING"]["source"], "missing")
        self.assertFalse(by_name["MISSING"]["defined"])

    def test_describe_marks_reserved(self):
        resolver = _resolver(org_data={}, global_data={})
        rows = resolver.describe("t1", ["PATH"])
        self.assertEqual(rows[0]["source"], "reserved")
        self.assertFalse(rows[0]["defined"])

    def test_global_describe_only_global_layer(self):
        resolver = _resolver(
            org_data={"t1": {"API_TOKEN": "org-value"}},
            global_data={"API_TOKEN": "global-secret-value-1234", "FEATURE": "on"},
        )
        rows = resolver.global_describe(["API_TOKEN", "FEATURE", "MISSING"])
        by_name = {r["name"]: r for r in rows}
        # Even though org defines API_TOKEN, the global view reports the
        # platform layer value, not the org override.
        self.assertEqual(by_name["API_TOKEN"]["source"], "global")
        self.assertEqual(by_name["API_TOKEN"]["masked"], "gl****34")
        self.assertEqual(by_name["FEATURE"]["source"], "global")
        self.assertEqual(by_name["MISSING"]["source"], "missing")


if __name__ == "__main__":
    import unittest

    unittest.main()
