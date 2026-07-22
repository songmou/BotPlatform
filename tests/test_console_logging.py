from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO

from src.core.infrastructure.logging import log_model_call, log_tool_call
from src.core.modeling import ModelIdentity, ModelUsage
from src.core.tooling import ToolAuditContext


class ConsoleLoggingTests(unittest.TestCase):
    def test_model_and_tool_logs_include_identity_metadata(self) -> None:
        output = StringIO()
        identity = ModelIdentity("cloud", "deepseek", "configured-model")
        context = ToolAuditContext(
            "o9cq800kum_secret@im.wechat", "deepseek", "cloud", "actual-model"
        )
        with redirect_stdout(output):
            log_model_call(
                identity,
                "actual-model",
                "成功",
                1.25,
                ModelUsage(10, 2, 12),
                1,
                "req-1",
            )
            log_tool_call(context, "list_directory", "成功", 0.006, 1701)
        text = output.getvalue()
        self.assertIn("提供商=deepseek | 档案=cloud | 模型=actual-model", text)
        self.assertIn("输入=10tok | 输出=2tok | 工具调用=1 | 请求=req-1", text)
        self.assertIn("工具=list_directory", text)
        self.assertNotIn("o9cq800kum_secret@im.wechat", text)


if __name__ == "__main__":
    unittest.main()
