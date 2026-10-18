
"""AronaClient 工具注册表。

将浏览器控制、应用启动、文件下载等能力注册为可调用工具，
供 AI 聊天流程通过结构化调用使用。

设计原则：
    * 工具名称唯一、描述清晰
    * 参数有类型校验和默认值
    * 返回值统一格式：{success, result, error, message}
    * 不直接暴露底层控制器，通过注册表统一管理
"""

from __future__ import annotations

from typing import Any, Callable

from .web_browser import BrowserController
from .app_launcher import AppLauncher
from .downloader import Downloader


# ============================================================
# 工具定义
# ============================================================

ToolDef = dict[str, Any]


def _make_tool_schema(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> ToolDef:
    """构建 OpenAI-compatible 工具 schema。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


# ============================================================
# 工具注册表
# ============================================================

class ToolRegistry:
    """
    工具注册表。

    管理所有可被 AI 调用的桌面操作工具。
    每个工具包含：schema（供模型理解）、handler（实际执行）。
    """

    def __init__(
        self,
        browser: BrowserController | None = None,
        launcher: AppLauncher | None = None,
        downloader: Downloader | None = None,
    ):
        self._tools: dict[str, ToolDef] = {}
        self._handlers: dict[str, Callable] = {}

        # 统一控制器实例
        self._browser = browser or BrowserController()
        self._launcher = launcher or AppLauncher()
        self._downloader = downloader or Downloader()

        self._register_builtins()

    def _register_builtins(self) -> None:
        """注册内置工具。"""
        self.register(
            schema=_make_tool_schema(
                name="open_url",
                description=(
                    "在默认浏览器中打开指定的网址。"
                    "只支持 http 和 https 协议。"
                    "用户必须明确告知要打开的网址。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": (
                                "要打开的完整网址，"
                                "例如 'https://example.com'"
                            ),
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            ),
            handler=self._browser.open_url,
        )

        self.register(
            schema=_make_tool_schema(
                name="launch_app",
                description=(
                    "启动应用程序。"
                    "可以使用应用名称（在 PATH 中查找）"
                    "或完整路径。"
                    "例如：'notepad'、'calc'、"
                    "'C:\\Program Files\\App\\app.exe'"
                    "不支持脚本文件（.bat/.ps1/.vbs 等）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name_or_path": {
                            "type": "string",
                            "description": (
                                "应用名称或完整路径。"
                            ),
                        },
                        "args": {
                            "type": "string",
                            "description": (
                                "命令行参数（可选）。"
                            ),
                        },
                        "workdir": {
                            "type": "string",
                            "description": (
                                "工作目录（可选）。"
                            ),
                        },
                    },
                    "required": ["name_or_path"],
                    "additionalProperties": False,
                },
            ),
            handler=self._launcher.launch,
        )

        self.register(
            schema=_make_tool_schema(
                name="download_file",
                description=(
                    "从 HTTPS 网址下载文件到本地。"
                    "默认保存到 ~/Downloads/arona_downloads/。"
                    "只支持 HTTPS，最大 100MB。"
                    "下载后不会自动执行文件。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "文件的 HTTPS 下载地址。",
                        },
                        "filename": {
                            "type": "string",
                            "description": (
                                "保存文件名（可选）。"
                                "不指定则自动命名。"
                            ),
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            ),
            handler=self._downloader.download,
        )

    def register(
        self,
        schema: ToolDef,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """
        注册一个新工具。

        参数：
            schema: OpenAI-compatible 工具定义。
            handler: 接受参数字典、返回结果字典的函数。
        """

        name = schema["function"]["name"]
        self._tools[name] = schema
        self._handlers[name] = handler

    def get_tools(self) -> list[ToolDef]:
        """返回所有已注册工具的 schema 列表。"""
        return list(self._tools.values())

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        调用指定工具。

        参数：
            name: 工具名称。
            arguments: 工具参数字典。

        返回：
            {
                "success": bool,
                "result": Any,
                "error": str | None,
                "message": str,
            }
        """

        if name not in self._handlers:
            return {
                "success": False,
                "result": None,
                "error": f"未知工具：{name}",
                "message": f"工具 '{name}' 不存在。",
            }

        try:
            result = self._handlers[name](**arguments)
        except TypeError as exc:
            return {
                "success": False,
                "result": None,
                "error": f"参数错误：{exc}",
                "message": "工具参数不匹配。",
            }
        except Exception as exc:
            return {
                "success": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "工具执行失败。",
            }

        # 标准化返回格式
        if isinstance(result, dict):
            if "success" not in result:
                result["success"] = True
            if "error" not in result:
                result["error"] = None
            if "message" not in result:
                result["message"] = str(result.get("result", ""))
            return result

        return {
            "success": True,
            "result": result,
            "error": None,
            "message": str(result),
        }

    def has_tool(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._handlers

    def tool_count(self) -> int:
        """返回已注册工具数量。"""
        return len(self._handlers)
