"""
spatial_ui.styles — 全局样式常量（L2 空间界面设计 token）。

视觉基准：Apple visionOS / 夜间樱色空间。
所有色值均为 QSS 可直接使用的字符串。
"""

# ============================================================
# 空间环境（深蓝紫夜空）
# ============================================================

COLOR_SPACE_TOP = "#141a33"
COLOR_SPACE_CENTER = "#1b2144"
COLOR_SPACE_EDGE = "#090c1c"

# ============================================================
# 玻璃材质
# ============================================================

# 主面板基底：深夜蓝紫半透明（对比度已按截图校验上调）
GLASS_OPACITY = 0.62
GLASS_RGB = (38, 45, 84)
COLOR_GLASS_BASE = "rgba(38, 45, 84, 196)"
# 高光边框（顶部亮、底部暗的立体感通过双边框模拟）
COLOR_GLASS_EDGE = "rgba(165, 185, 255, 0.34)"
COLOR_GLASS_EDGE_HOVER = "rgba(175, 198, 255, 0.58)"
# 面板内侧顶部高光
COLOR_GLASS_HIGHLIGHT = "rgba(255, 255, 255, 0.09)"

# 内嵌小控件（输入框、次级卡片）
COLOR_INSET = "rgba(18, 23, 46, 0.72)"
COLOR_INSET_BORDER = "rgba(140, 160, 235, 0.16)"

# ============================================================
# 强调色
# ============================================================

COLOR_ACCENT_BLUE = "#7b93ff"        # 主强调（导航选中、按钮）
COLOR_ACCENT_BLUE_DEEP = "#5566e8"
COLOR_ACCENT_PURPLE = "#a78bfa"      # 辅助强调
COLOR_ACCENT_PINK = "#f0a6c8"        # 樱色点缀
COLOR_SUCCESS = "#5fd6a5"
COLOR_WARNING = "#f0bb63"
COLOR_ERROR = "#f2728f"

# ============================================================
# 文字
# ============================================================

COLOR_TEXT_PRIMARY = "#eef1ff"
COLOR_TEXT_SECONDARY = "#c3c9e8"
COLOR_TEXT_MUTED = "#8b92b8"
COLOR_TEXT_DIM = "#666d92"

# ============================================================
# 选中 / 悬停
# ============================================================

COLOR_SELECTION_GLOW = "rgba(123, 147, 255, 0.22)"
COLOR_HOVER_BG = "rgba(255, 255, 255, 0.06)"
COLOR_ACTIVE_PILL = "rgba(123, 147, 255, 0.24)"

# ============================================================
# 圆角
# ============================================================

BUBBLE_RADIUS = 16
PANEL_RADIUS = 22
CARD_RADIUS = 18
PILL_RADIUS = 16
WINDOW_RADIUS = 24

# ============================================================
# 阴影
# ============================================================

SHADOW_BLUR = 28
SHADOW_OFFSET = 6
SHADOW_COLOR = "rgba(4, 8, 24, 0.55)"

# ============================================================
# 尺寸规范
# ============================================================

MIN_WINDOW_WIDTH = 1180
MIN_WINDOW_HEIGHT = 720
RECOMMENDED_WINDOW_WIDTH = 1366
RECOMMENDED_WINDOW_HEIGHT = 768

SIDEBAR_EXPANDED_WIDTH = 248
SIDEBAR_COLLAPSED_WIDTH = 76
TOPBAR_HEIGHT = 66
BOTTOMBAR_HEIGHT = 36

COMPOSER_HEIGHT = 56
COMPOSER_MAX_HEIGHT = 180

# 面板外边距（空间感：面板不贴边）
PANEL_MARGIN = 14
# 面板间距
PANEL_SPACING = 14

# ============================================================
# 字体
# ============================================================

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"'
FONT_MONO = '"Cascadia Mono", "Consolas", "Courier New"'

# ============================================================
# 面板体系（统一的主/信息/快捷/提示面板规格）
# ============================================================

PANEL_KINDS = {
    # 主面板：侧栏、聊天主面板
    "main": {"radius": 22, "fill_alpha": 198, "border_alpha": 88, "shadow": True},
    # 信息面板：右侧信息卡、系统状态卡
    "info": {"radius": 18, "fill_alpha": 194, "border_alpha": 74, "shadow": True},
    # 快捷面板：悬浮快捷栏、上下文菜单
    "quick": {"radius": 26, "fill_alpha": 208, "border_alpha": 96, "shadow": True},
    # 提示面板：Toast / 状态条
    "toast": {"radius": 14, "fill_alpha": 214, "border_alpha": 66, "shadow": False},
}

# 聊天内容滚动渐隐高度（px）
FADE_TOP_HEIGHT = 58
FADE_BOTTOM_HEIGHT = 34

# 侧栏隐藏后的边缘恢复入口
EDGE_HANDLE_W = 16
EDGE_HANDLE_H = 66

# 侧栏展开/收起动画
SIDEBAR_ANIM_MS = 180

# ============================================================
# 性能模式
# ============================================================

PERFORMANCE_MODES = {
    "high": {
        "particles": True,
        "blur": True,
        "blur_radius": 10,
        "shadow": True,
        "glow": True,
        "anim_speed": 1.0,
        "shadow_blur": SHADOW_BLUR,
        "glass_alpha": 178,
    },
    "medium": {
        "particles": False,
        "blur": True,
        "blur_radius": 6,
        "shadow": True,
        "glow": True,
        "anim_speed": 0.7,
        "shadow_blur": 18,
        "glass_alpha": 192,
    },
    "low": {
        "particles": False,
        "blur": False,
        "blur_radius": 0,
        "shadow": False,
        "glow": False,
        "anim_speed": 0.5,
        "shadow_blur": 0,
        "glass_alpha": 206,
    },
}
