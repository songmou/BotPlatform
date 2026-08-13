"""真实 stdio MCP server，供集成测试以真实协议连接。

暴露三个工具：
- ``echo(text)``   回显输入文本
- ``add(a, b)``     返回两个整数之和
- ``boom()``        故意抛错，用于验证 MCP 错误反馈链路

由 ``McpClientManager`` 以 stdio transport 连接：
    command = sys.executable
    args    = [本文件]
工具名在智能体侧被命名为 ``local_echo__echo`` / ``local_echo__add`` / ``local_echo__boom``
（server id 为 ``local_echo``）。
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("local_echo")


@mcp.tool()
def echo(text: str) -> str:
    """回显输入的文本，用于验证 stdio 链路往返。"""
    return "echo:{}".format(text)


@mcp.tool()
def add(a: int, b: int) -> int:
    """返回两个整数之和，用于验证带参工具调用。"""
    return a + b


@mcp.tool()
def boom() -> str:
    """故意抛错，用于验证 MCP 工具错误（isError）如何反馈给调用方。"""
    raise RuntimeError("boom: 故意触发的错误")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
