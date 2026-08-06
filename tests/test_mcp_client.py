"""Tests for MCP client error formatting helpers.

These focus on ``_format_mcp_error``, which unwraps anyio/exceptiongroup
ExceptionGroups so operators see the real connection failure instead of the
opaque "unhandled errors in a TaskGroup (N sub-exceptions)" message.
"""

import unittest


def _exception_group_type():
    """Return the ExceptionGroup class available in the running interpreter.

    Python 3.11+ exposes ``ExceptionGroup`` as a builtin; older interpreters
    rely on the ``exceptiongroup`` backport shipped with anyio's deps.
    """
    try:
        from builtins import ExceptionGroup  # type: ignore  # noqa: F401

        return ExceptionGroup
    except ImportError:  # pragma: no cover - depends on interpreter version
        from exceptiongroup import ExceptionGroup  # type: ignore

        return ExceptionGroup


class FormatMcpErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        from src.core.tooling.mcp_client import _format_mcp_error

        self.format = _format_mcp_error
        self.ExceptionGroup = _exception_group_type()

    def test_plain_exception_returns_message(self) -> None:
        self.assertEqual(self.format(ValueError("boom")), "boom")

    def test_empty_message_falls_back_to_type(self) -> None:
        self.assertEqual(self.format(ValueError()), "ValueError")

    def test_unwraps_single_sub_exception(self) -> None:
        wrapped = self.ExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [ConnectionError("401 Unauthorized")],
        )
        msg = self.format(wrapped)
        self.assertIn("401 Unauthorized", msg)
        self.assertNotIn("unhandled errors in a TaskGroup", msg)

    def test_unwraps_multiple_sub_exceptions(self) -> None:
        wrapped = self.ExceptionGroup(
            "unhandled errors in a TaskGroup (2 sub-exceptions)",
            [ConnectionError("401 Unauthorized"), TimeoutError("read timeout")],
        )
        msg = self.format(wrapped)
        self.assertIn("401 Unauthorized", msg)
        self.assertIn("read timeout", msg)

    def test_unwraps_nested_exception_groups(self) -> None:
        inner = self.ExceptionGroup(
            "inner", [ConnectionError("401 Unauthorized")]
        )
        outer = self.ExceptionGroup("outer task group", [inner])
        msg = self.format(outer)
        self.assertIn("401 Unauthorized", msg)
        self.assertIn("ExceptionGroup", msg)


if __name__ == "__main__":
    unittest.main()
