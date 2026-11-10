"""
spatial_ui — Vision Pro Inspired Spatial UI for AronaClient.

空间交互界面包，提供：
  - 玻璃面板组件 (GlassPanel)
  - 空间窗口管理器 (SpatialWindowManager)
  - 空间背景渲染 (SpaceBackground)
  - 新侧边栏 / 顶栏 / 聊天气泡 / Composer / 模型舞台
  - 全局样式常量与动画参数

所有组件均向后兼容原有 ui.py 接口。
渐进式迁移：新组件可逐步替换旧实现，不影响现有功能。
"""

from spatial_ui.styles import (
    COLOR_SPACE_CENTER,
    COLOR_SPACE_EDGE,
    COLOR_GLASS_BASE,
    COLOR_GLASS_EDGE,
    COLOR_ACCENT_BLUE,
    COLOR_ACCENT_PURPLE,
    COLOR_SUCCESS,
    COLOR_WARNING,
    COLOR_ERROR,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    COLOR_SELECTION_GLOW,
    GLASS_OPACITY,
    BUBBLE_RADIUS,
    PANEL_RADIUS,
    WINDOW_RADIUS,
    SHADOW_BLUR,
    SHADOW_OFFSET,
    PERFORMANCE_MODES,
)
from spatial_ui.animations import (
    ANIM_SIDEBAR_DURATION,
    ANIM_BUBBLE_DURATION,
    ANIM_WINDOW_SHOW_DURATION,
    ANIM_WINDOW_CLOSE_DURATION,
    ANIM_HOVER_DURATION,
    ANIM_TOAST_DURATION,
)

__all__ = [
    # Styles
    "COLOR_SPACE_CENTER",
    "COLOR_SPACE_EDGE",
    "COLOR_GLASS_BASE",
    "COLOR_GLASS_EDGE",
    "COLOR_ACCENT_BLUE",
    "COLOR_ACCENT_PURPLE",
    "COLOR_SUCCESS",
    "COLOR_WARNING",
    "COLOR_ERROR",
    "COLOR_TEXT_MUTED",
    "COLOR_TEXT_PRIMARY",
    "COLOR_SELECTION_GLOW",
    "GLASS_OPACITY",
    "BUBBLE_RADIUS",
    "PANEL_RADIUS",
    "WINDOW_RADIUS",
    "SHADOW_BLUR",
    "SHADOW_OFFSET",
    "PERFORMANCE_MODES",
    # Animations
    "ANIM_SIDEBAR_DURATION",
    "ANIM_BUBBLE_DURATION",
    "ANIM_WINDOW_SHOW_DURATION",
    "ANIM_WINDOW_CLOSE_DURATION",
    "ANIM_HOVER_DURATION",
    "ANIM_TOAST_DURATION",
]
