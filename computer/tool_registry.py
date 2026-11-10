
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

        # ----------------------------------------------------
        # 只读桌面感知工具
        # ----------------------------------------------------
        #
        # 这两个工具只读取桌面状态，不移动鼠标、不输入键盘、
        # 不点击任何控件。它们与 open_url / launch_app / download_file
        # 这类有副作用的工具在权限边界上明确分开：
        #   * 只读工具仍然受 computer.desktop_access 门禁约束，
        #     未授权时返回明确的失败信息，不会伪造桌面状态；
        #   * 有副作用工具各自有独立的安全校验。
        self.register(
            schema=_make_tool_schema(
                name="get_active_window",
                description=(
                    "读取当前前台活动窗口的信息（标题、进程名、"
                    "窗口位置与尺寸）。只读操作，不会操作窗口。"
                    "需要用户已开启桌面视觉授权。"
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            handler=self._get_active_window_tool,
        )

        self.register(
            schema=_make_tool_schema(
                name="list_windows",
                description=(
                    "列出当前桌面上可见且有标题的窗口（最多 20 个，"
                    "上限 200）。只读操作。"
                    "不返回进程路径或窗口句柄等内部标识。"
                    "需要用户已开启桌面视觉授权。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": (
                                "最多返回多少个窗口，默认 20，上限 200。"
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            handler=self._list_windows_tool,
        )

    # ========================================================
    # 只读桌面感知工具 handler
    # ========================================================
    #
    # 统一返回 {success, result, error, message}，
    # 与 ToolRegistry.call_tool 的标准化约定一致。
    # 失败时绝不编造桌面状态：result 一律为 None。

    def _get_active_window_tool(self) -> dict[str, Any]:
        """读取前台窗口。未授权或读取失败时返回明确失败。"""
        from .window_info import get_active_window

        try:
            raw = get_active_window()
        except Exception as exc:
            return {
                "success": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "读取活动窗口失败。",
            }

        if not isinstance(raw, dict) or not raw.get("success"):
            reason = ""
            if isinstance(raw, dict):
                reason = raw.get("error") or ""
            return {
                "success": False,
                "result": None,
                "error": reason or "无法读取活动窗口。",
                "message": f"读取活动窗口失败：{reason or '未知原因'}",
            }

        window = raw.get("window") or {}

        # 最小必要原则：只回传模型理解桌面所需的字段，
        # 不暴露窗口句柄与进程完整路径。
        summary = {
            "title": window.get("title") or "",
            "process_name": self._process_name(window.get("process_path")),
            "x": window.get("x"),
            "y": window.get("y"),
            "width": window.get("width"),
            "height": window.get("height"),
            "visible": window.get("visible"),
            "minimized": window.get("minimized"),
        }

        title = summary["title"] or "（无标题窗口）"

        return {
            "success": True,
            "result": summary,
            "error": None,
            "message": f"当前活动窗口：{title}",
        }

    def _list_windows_tool(self, limit=None) -> dict[str, Any]:
        """枚举可见窗口。参数非法或未授权时返回明确失败。"""
        from .window_info import (
            DEFAULT_WINDOW_LIMIT,
            list_visible_windows,
        )

        if limit is None:
            limit = DEFAULT_WINDOW_LIMIT

        try:
            windows = list_visible_windows(limit=limit)
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": f"参数无效：{exc}",
            }
        except PermissionError as exc:
            return {
                "success": False,
                "result": None,
                "error": str(exc),
                "message": f"读取窗口列表失败：{exc}",
            }
        except Exception as exc:
            return {
                "success": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "读取窗口列表失败。",
            }

        return {
            "success": True,
            "result": list(windows),
            "error": None,
            "message": f"当前共有 {len(windows)} 个可见窗口。",
        }

    @staticmethod
    def _process_name(process_path):
        """从完整路径取进程文件名；不把完整路径交给模型。"""
        if not isinstance(process_path, str) or not process_path:
            return ""
        try:
            import os
            return os.path.basename(process_path)
        except Exception:
            return ""

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
