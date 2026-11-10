# -*- coding: utf-8 -*-
# Copyright (C) 2024-2026 SorasakiHina-219
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.


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
