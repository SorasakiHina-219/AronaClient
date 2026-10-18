
"""AronaClient Computer Use：本地桌面操作模块。

包含：
    - screen_capture     : 屏幕截图（Qt）
    - window_info        : 窗口信息（Win32）
    - desktop_access     : 授权门禁
    - web_browser        : 浏览器导航（V0.2 新增）
    - app_launcher       : 应用启动（V0.2 新增）
    - downloader         : 文件下载（V0.2 新增）
    - tool_registry      : 工具注册表（V0.2 新增）
"""

from .screen_capture import ScreenCapture
from .window_info import WindowInfo, get_active_window, list_visible_windows
from .desktop_access import (
    set_provider,
    clear_provider,
    check,
    is_authorized,
    note_denied,
)
from .web_browser import BrowserController
from .app_launcher import AppLauncher
from .downloader import Downloader
from .tool_registry import ToolRegistry

__all__ = [
    "ScreenCapture",
    "WindowInfo",
    "get_active_window",
    "list_visible_windows",
    "set_provider",
    "clear_provider",
    "check",
    "is_authorized",
    "note_denied",
    "BrowserController",
    "AppLauncher",
    "Downloader",
    "ToolRegistry",
]
