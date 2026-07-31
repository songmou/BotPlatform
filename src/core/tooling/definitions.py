"""Built-in tool schemas exposed to agents by the tool runtime.

Pure declarations only: the ``_tool_{name}`` handlers implementing these
tools live on ``ToolRuntime`` in ``runtime.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


APPROVAL_TOOLS = {
    "create_directory",
    "write_text_file",
    "replace_text",
    "copy_path",
    "move_path",
    "move_to_trash",
    "run_command",
    "run_script",
    "cancel_script_run",
    "manage_script_schedule",
    "knowledge_add_text",
    "knowledge_index_file",
    "knowledge_delete",
    "drive_delete_file",
}


def _object_schema(
    properties: Optional[Dict[str, Any]] = None,
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "ocr_extract_text": {
        "description": (
            "识别当前用户 workspace 内图片或 PDF 中的文字。"
            "支持 JPEG、PNG、WebP、BMP、GIF 首帧和最多 10 页的 PDF。"
        ),
        "parameters": _object_schema(
            {"path": {"type": "string", "description": "workspace 内的文件路径"}},
            ["path"],
        ),
    },
    "knowledge_add_text": {
        "description": "把用户明确提供的纯文本保存到当前用户的私人知识库。",
        "parameters": _object_schema(
            {"name": {"type": "string"}, "content": {"type": "string"}},
            ["name", "content"],
        ),
    },
    "knowledge_index_file": {
        "description": (
            "索引当前用户 workspace 内的知识文件，"
            "支持 TXT、Markdown、PDF、Word(docx)、Excel(xlsx)、PPT(pptx)。"
        ),
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
    "knowledge_search": {
        "description": "检索当前智能体已绑定、且当前用户可见的知识库。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                },
                "category_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                },
            },
            ["query"],
        ),
    },
    "knowledge_list": {
        "description": "列出当前用户的知识来源及索引状态。",
        "parameters": _object_schema(),
    },
    "knowledge_delete": {
        "description": "按来源编号删除当前用户的一项知识及其索引。",
        "parameters": _object_schema({"source_id": {"type": "string"}}, ["source_id"]),
    },
    "list_allowed_roots": {
        "description": "显示本机工具允许访问的根目录和当前默认工作目录。",
        "parameters": _object_schema(),
    },
    "list_directory": {
        "description": "列出目录中的文件和子目录；相对路径基于默认工作目录。",
        "parameters": _object_schema(
            {
                "path": {"type": "string", "description": "目录路径，默认 ."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
            }
        ),
    },
    "find_files": {
        "description": "在目录中递归按文件名查找文件或目录，支持 * 和 ? 通配符。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "默认 ."},
                "max_results": {"type": "integer", "minimum": 1},
            },
            ["query"],
        ),
    },
    "search_text": {
        "description": "在开放目录的 UTF-8 文本文件中搜索文字。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "默认 ."},
                "glob": {"type": "string", "description": "可选文件名模式，如 *.py"},
                "case_sensitive": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1},
            },
            ["query"],
        ),
    },
    "read_text_file": {
        "description": "按行读取开放目录中的 UTF-8 文本文件。",
        "parameters": _object_schema(
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 400},
            },
            ["path"],
        ),
    },
    "get_path_info": {
        "description": "获取文件或目录的类型、大小和修改时间。",
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
    "get_current_time": {
        "description": "获取机器人配置时区中的当前日期和时间。",
        "parameters": _object_schema(),
    },
    "get_system_info": {
        "description": "获取本机操作系统、架构和主机名，不返回环境变量。",
        "parameters": _object_schema(),
    },
    "get_disk_usage": {
        "description": "获取开放目录所在磁盘的容量和可用空间。",
        "parameters": _object_schema({"path": {"type": "string"}}),
    },
    "list_processes": {
        "description": "列出本机进程的 PID 和程序名，不包含完整命令参数。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
    },
    "create_directory": {
        "description": "新建目录，需要用户确认。",
        "parameters": _object_schema(
            {"path": {"type": "string"}, "parents": {"type": "boolean"}}, ["path"]
        ),
    },
    "write_text_file": {
        "description": "新建或覆盖 UTF-8 文本文件，需要用户确认。",
        "parameters": _object_schema(
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite"]},
            },
            ["path", "content", "mode"],
        ),
    },
    "replace_text": {
        "description": "在文本文件中精确替换内容，需要用户确认。",
        "parameters": _object_schema(
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "expected_count": {"type": "integer", "minimum": 1},
            },
            ["path", "old_text", "new_text"],
        ),
    },
    "copy_path": {
        "description": "复制文件或目录到一个不存在的目标路径，需要用户确认。",
        "parameters": _object_schema(
            {"source": {"type": "string"}, "destination": {"type": "string"}},
            ["source", "destination"],
        ),
    },
    "move_path": {
        "description": "移动文件或目录到一个不存在的目标路径，需要用户确认。",
        "parameters": _object_schema(
            {"source": {"type": "string"}, "destination": {"type": "string"}},
            ["source", "destination"],
        ),
    },
    "move_to_trash": {
        "description": "把文件或目录移到 iLinkBot 专用废纸篓，不会永久删除，需要用户确认。",
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
    "run_command": {
        "description": "使用白名单档案在 macOS 沙箱中运行命令，需要用户确认；不支持 shell 字符串。",
        "parameters": _object_schema(
            {
                "profile": {
                    "type": "string",
                    "enum": [
                        "python", "git_readonly", "node", "npm_script",
                        "ollama_readonly", "workspace_script"
                    ],
                },
                "args": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            ["profile", "args"],
        ),
    },
    "list_scripts": {
        "description": "列出 iLinkBot 已注册、可由模型请求运行的固定脚本及其参数。",
        "parameters": _object_schema(),
    },
    "run_script": {
        "description": (
            "提交已注册的固定脚本到后台异步运行，立即返回任务编号（状态通常为 running）。"
            "脚本在后台执行，完成后其结果摘要和产物会自动推送给用户，无需你在对话中等待。"
            "提交成功后应直接告知用户“已提交，结果将在完成后自动发送”，"
            "不要反复调用 get_script_run 轮询等待完成（会耗尽工具调用轮次）。"
        ),
        "parameters": _object_schema(
            {
                "script_id": {"type": "string"},
                "parameters": {
                    "type": "object",
                    "description": "脚本的具名参数；先用 list_scripts 查看允许值。",
                    "additionalProperties": True,
                },
            },
            ["script_id"],
        ),
    },
    "get_script_run": {
        "description": (
            "仅在用户明确要求查询某个任务编号的当前状态时，做一次性状态查询；"
            "不要用它轮询等待脚本完成，脚本结果会在完成后自动推送。"
        ),
        "parameters": _object_schema(
            {"run_id": {"type": "string"}}, ["run_id"]
        ),
    },
    "cancel_script_run": {
        "description": "取消当前用户仍在运行的脚本任务，需要用户确认。",
        "parameters": _object_schema(
            {"run_id": {"type": "string"}}, ["run_id"]
        ),
    },
    "list_script_schedules": {
        "description": "列出当前用户已创建的无人值守脚本定时计划。",
        "parameters": _object_schema(),
    },
    "manage_script_schedule": {
        "description": (
            "创建、更新、启用、停用或删除当前用户的无人值守脚本计划。"
            "所有变更都需要用户确认；创建或重新启用时会固定当前脚本版本。"
        ),
        "parameters": _object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "enable", "disable", "delete"],
                },
                "schedule_id": {"type": "string"},
                "script_id": {"type": "string"},
                "parameters": {
                    "type": "object",
                    "additionalProperties": True,
                },
                "crons": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "enabled": {"type": "boolean"},
            },
            ["action", "schedule_id"],
        ),
    },
    "drive_list_files": {
        "description": (
            "列出网盘目录：scope=tenant 为当前用户的个人网盘，"
            "scope=public 为全局公共文件区（只读）。"
        ),
        "parameters": _object_schema(
            {
                "scope": {"type": "string", "enum": ["tenant", "public"]},
                "path": {"type": "string", "description": "相对目录，默认根目录"},
            }
        ),
    },
    "drive_read_file": {
        "description": "读取网盘中的 UTF-8 文本文件，支持个人网盘和公共区。",
        "parameters": _object_schema(
            {
                "scope": {"type": "string", "enum": ["tenant", "public"]},
                "path": {"type": "string"},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 400},
            },
            ["path"],
        ),
    },
    "drive_save_file": {
        "description": (
            "把文本内容保存到当前用户的个人网盘（公共区只读，无法写入）。"
        ),
        "parameters": _object_schema(
            {
                "path": {"type": "string", "description": "相对文件路径，如 workspace/notes.txt"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            ["path", "content"],
        ),
    },
    "drive_delete_file": {
        "description": "删除当前用户个人网盘中的文件或空目录，需要用户确认。",
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
}
