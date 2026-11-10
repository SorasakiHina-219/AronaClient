"""
spatial_ui.animations — 动画参数常量。

所有动画时长（毫秒）和缓动函数名。
使用 QPropertyAnimation 时引用这些常量。
"""

# ============================================================
# 动画时长（ms）
# ============================================================

# 侧边栏展开/收起
ANIM_SIDEBAR_DURATION = 200
# 消息气泡出现
ANIM_BUBBLE_DURATION = 300
# 窗口打开淡入
ANIM_WINDOW_SHOW_DURATION = 200
# 窗口关闭淡出
ANIM_WINDOW_CLOSE_DURATION = 150
# 按钮悬停效果
ANIM_HOVER_DURATION = 150
# Toast 通知展示时长
ANIM_TOAST_DURATION = 3000
# Toast 进出动画时长
ANIM_TOAST_TRANSITION_DURATION = 250

# ============================================================
# 缓动函数（Qt EasingCurve 名称）
# ============================================================

# 平滑 ease-out（适合打开/显示）
EASING_OUT_CUBIC = "OutCubic"
# 平滑 ease-in-out（适合过渡）
EASING_IN_OUT_CUBIC = "InOutCubic"
# 快速 ease-out（适合悬停）
EASING_OUT_QUAD = "OutQuad"
# 弹性 ease-out（适合重要动作）
EASING_OUT_BOUNCE = "OutBack"

# ============================================================
# 帧率控制
# ============================================================

# 背景粒子动画最大帧率（fps）
MAX_PARTICLE_FPS = 15
# 粒子动画间隔（ms）
PARTICLE_FRAME_INTERVAL = int(1000 / MAX_PARTICLE_FPS)
