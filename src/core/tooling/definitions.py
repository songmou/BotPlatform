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
    "db_execute",
    "git",
}


#: Read-only datasource tools.  These are granted implicitly to any agent that
#: has at least one datasource bound — the "数据源" tab is the single entry
#: point, so they are never listed in the built-in tool picker.
DATASOURCE_READONLY_TOOLS = (
    "db_list_tables",
    "db_describe_table",
    "db_query",
)

#: Every datasource-scoped tool, read-only plus write.  Used to hide db_* from
#: the built-in tool pickers and to enforce the datasource binding at runtime.
DATASOURCE_TOOLS = DATASOURCE_READONLY_TOOLS + ("db_execute",)


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
        "description": (
            "列出当前空间/组织已配置的定时任务（含 Web 面板配置的任务）。"
            "返回每条任务的编号、是否启用、cron 时间、动作类型与脚本信息。"
        ),
        "parameters": _object_schema(),
    },
    "manage_script_schedule": {
        "description": (
            "创建、更新、启用、停用或删除当前空间/组织的定时任务。"
            "所有变更都需要用户确认；只有组织所有者或管理员可以修改。"
            "新建或更新只能操作脚本（script）类型的任务。"
        ),
        "parameters": _object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "enable", "disable", "delete"],
                },
                "schedule_id": {
                    "type": "string",
                    "description": "任务编号；新建时即为新任务的编号",
                },
                "script_id": {
                    "type": "string",
                    "description": "脚本任务引用的平台脚本 ID（仅 script 类型需要）",
                },
                "parameters": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "传递给脚本的参数",
                },
                "crons": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                    "description": "五段 cron 表达式，如 ['50 9 * * *', '50 17 * * *']",
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
    "db_list_tables": {
        "description": "列出当前智能体已授权数据源中的所有可查询表。请先使用此工具了解可用数据。",
        "parameters": _object_schema(
            {
                "datasource_id": {
                    "type": "string",
                    "description": "数据源 ID，请从系统提示词中查找可用的数据源。",
                },
            },
            ["datasource_id"],
        ),
    },
    "db_describe_table": {
        "description": "查看指定表的字段名、类型、注释与主键信息。表名来自 db_list_tables 返回结果。",
        "parameters": _object_schema(
            {
                "datasource_id": {"type": "string"},
                "table": {"type": "string", "description": "表名"},
            },
            ["datasource_id", "table"],
        ),
    },
    "db_query": {
        "description": (
            "对已授权数据源执行只读 SELECT 查询。"
            "仅允许单条 SELECT 语句，结果自动限制行数与字节数。"
            "请先在系统提示词或 db_describe_table 中确认表结构后再编写 SQL。"
        ),
        "parameters": _object_schema(
            {
                "datasource_id": {"type": "string"},
                "sql": {
                    "type": "string",
                    "maxLength": 4000,
                    "description": "要执行的 SELECT 语句",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "限制返回行数，默认使用数据源配置的上限",
                },
            },
            ["datasource_id", "sql"],
        ),
    },
    "db_execute": {
        "description": (
            "对已开启写权限的数据源执行单条 INSERT/UPDATE/DELETE 操作，需用户确认。"
            "UPDATE 和 DELETE 必须包含 WHERE 条件。"
            "请提供执行原因。"
        ),
        "parameters": _object_schema(
            {
                "datasource_id": {"type": "string"},
                "sql": {
                    "type": "string",
                    "maxLength": 4000,
                    "description": "要执行的 INSERT/UPDATE/DELETE 语句",
                },
                "reason": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "执行原因，供用户审批时了解",
                },
            },
            ["datasource_id", "sql", "reason"],
        ),
    },
    "git": {
        "description": (
            "在平台管理的 Git 仓库中执行 Git 操作（subprocess 列表参数调用，非 shell）。"
            "支持 init/clone/status/log/diff/show/add/commit/push/pull/branch/"
            "checkout/grep/remote/fetch。"
            "首次获取远程代码必须用 clone；pull/fetch 只能用于已克隆过的仓库。"
            "clone/pull/fetch 只写沙箱目录，可直接执行；"
            "init/add/commit/push/checkout/remote 以及创建或删除分支需要用户审批。"
            "仓库必须在 git 配置根目录内；远程地址仅支持 HTTPS。"
            "clone 默认浅克隆（--depth=1），需要完整历史请显式传 --no-single-branch。"
            "返回结果中的 repo 是仓库在文件库中的位置（如 workspace/git_repos/code-reviewer）；"
            "回复用户时用该路径说明存放位置，不要提及服务器本地路径。"
        ),
        "parameters": _object_schema(
            {
                "command": {
                    "type": "string",
                    "enum": [
                        "init", "clone", "status", "log", "diff", "show",
                        "add", "commit", "push", "pull", "branch", "checkout",
                        "grep", "remote", "fetch",
                    ],
                    "description": "Git 子命令（仅白名单内）",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": (
                        "传递给 git 子命令的参数列表（字符串数组）。"
                        "clone 时只放仓库地址，不要写目标目录——目标目录由 repo_path 决定。"
                    ),
                },
                "repo_path": {
                    "type": "string",
                    "description": (
                        "仓库目录名，如 code-reviewer。平台会自动放到 git 根目录下，"
                        "不要带任何上级目录（例如不要写 git_repos/code-reviewer）。"
                        "clone 时为新建仓库的目标目录，其他命令时为已有仓库目录。"
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "命令超时秒数，默认 60",
                },
            },
            ["command", "repo_path"],
        ),
    },
}
