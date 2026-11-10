"""
spatial_ui.glass_panel — 玻璃面板组件（QPainter 真实绘制）。

为什么不用 QSS 画玻璃？
  - QSS 的 background/border-radius 在子类 setStyleSheet 时会相互覆盖，
    多个面板复用同一 objectName 时边框会消失。
  - QSS 无法绘制"顶部高光 + 底部加深"的立体玻璃层次。
本实现改为 paintEvent 手绘：圆角裁剪 + 双段渐变 + 内高光描边 + 可选外阴影。

性能：
  - low 模式：无阴影、无高光增强、纯填充（1 次填充 + 1 次描边）。
  - medium：填充 + 描边 + 轻阴影。
  - high：额外顶部光晕 + 更柔阴影。
"""

import warnings
warnings.filterwarnings("ignore")

from PySide6.QtCore import Qt, QRectF, Property
from PySide6.QtGui import (
    QPainter,
    QColor,
    QBrush,
    QPen,
    QLinearGradient,
    QPainterPath,
)
from PySide6.QtWidgets import QFrame

from spatial_ui.styles import (
    PANEL_RADIUS,
    SHADOW_BLUR,
    SHADOW_OFFSET,
    PANEL_KINDS,
    PERFORMANCE_MODES,
)


class GlassPanel(QFrame):
    """半透明玻璃面板基类。

    kind: "main" / "info" / "quick" / "toast"（统一样式体系）
    blur_provider: callable(widget) -> QPixmap，返回**背景层**模糊贴片，
                   隔离输入（只取 SpaceBackground 渐变，不含任何 UI）。
    """

    def __init__(
        self,
        parent=None,
        *,
        kind: str = "main",
        radius: int = None,
        tint: tuple = (38, 45, 84),
        fill_alpha: int = None,
        border_alpha: int = None,
        shadow_enabled: bool = None,
        shadow_blur: int = 18,
        performance_mode: str = "medium",
        hover_highlight: bool = True,
        blur_provider=None,
    ):
        super().__init__(parent)

        spec = dict(PANEL_KINDS.get(kind, PANEL_KINDS["main"]))
        self._kind = kind
        self._radius = int(radius if radius is not None else spec["radius"])
        self._tint = tint
        self._fill_alpha = int(
            fill_alpha if fill_alpha is not None else spec["fill_alpha"]
        )
        self._border_alpha = int(
            border_alpha if border_alpha is not None else spec["border_alpha"]
        )
        self._shadow_enabled = bool(
            spec["shadow"] if shadow_enabled is None else shadow_enabled
        )
        self._blur_provider = blur_provider
        self._blur_pm = None
        self._blur_key = None

        self._hovered = False
        self._hover_highlight = hover_highlight

        self._perf_mode = performance_mode
        self._perf = PERFORMANCE_MODES.get(
            performance_mode, PERFORMANCE_MODES["medium"]
        )
        if not self._perf.get("glow", False):
            self._hover_highlight = False

        # 透明绘制
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        # 阴影（只在允许时创建，避免低端机常驻 offscreen buffer）
        self._shadow_effect = None
        if shadow_enabled and self._perf.get("shadow", False):
            from PySide6.QtWidgets import QGraphicsDropShadowEffect

            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(shadow_blur or SHADOW_BLUR)
            eff.setOffset(0, SHADOW_OFFSET)
            eff.setColor(QColor(4, 8, 24, 150))
            self.setGraphicsEffect(eff)
            self._shadow_effect = eff

    # ── 属性 ──────────────────────────────────────────────

    def get_radius(self) -> int:
        return self._radius

    def set_radius(self, radius: int):
        self._radius = max(0, int(radius))
        self.update()

    radius = Property(int, get_radius, set_radius)

    def get_fill_alpha(self) -> int:
        return self._fill_alpha

    def set_fill_alpha(self, alpha: int):
        self._fill_alpha = max(0, min(255, int(alpha)))
        self.update()

    fill_alpha = Property(int, get_fill_alpha, set_fill_alpha)

    def set_performance_mode(self, mode: str):
        if mode not in PERFORMANCE_MODES:
            return
        self._perf_mode = mode
        self._perf = PERFORMANCE_MODES[mode]
        if self._shadow_effect is not None:
            self._shadow_effect.setEnabled(self._perf.get("shadow", False))
        self.refresh_blur()
        self.update()

    # ── 背景模糊（隔离输入：只取背景层贴片）───────────────

    def set_blur_provider(self, provider):
        self._blur_provider = provider
        self.refresh_blur()

    def refresh_blur(self):
        """重新取背景层模糊贴片（resize / 换模式 / 移动时调用）。"""
        pm = None
        key = None
        if self._blur_provider is not None and self._perf.get("blur", False):
            try:
                key = (self.width(), self.height(),
                       self.mapToGlobal(self.rect().topLeft()).x(),
                       self.mapToGlobal(self.rect().topLeft()).y(),
                       self._perf.get("blur_radius", 6))
                if key != self._blur_key or self._blur_pm is None:
                    pm = self._blur_provider(self)
                    self._blur_key = key
                    self._blur_pm = pm
                else:
                    pm = self._blur_pm
            except Exception:
                pm = None
        if pm is None:
            self._blur_pm = None
        self.update()

    # ── 绘制 ──────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        r = self._radius
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        # 0) 背景层模糊贴片（隔离输入；low 模式无）
        if self._blur_pm is not None and not self._blur_pm.isNull():
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(self.rect(), self._blur_pm)
            painter.restore()

        # 1) 主体：上亮下暗的垂直渐变（模拟玻璃厚度）
        tint_r, tint_g, tint_b = self._tint
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        top = QColor(tint_r + 10, tint_g + 12, tint_b + 22, self._fill_alpha)
        bottom = QColor(
            max(0, tint_r - 10),
            max(0, tint_g - 10),
            max(0, tint_b - 12),
            min(255, self._fill_alpha + 16),
        )
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bottom)
        painter.fillPath(path, QBrush(grad))

        # 2) 顶部内侧高光带（玻璃反光）
        if self._perf.get("glow", False):
            band = min(26.0, max(6.0, rect.height() * 0.20))
            hl = QLinearGradient(
                rect.left(), rect.top(),
                rect.left(), rect.top() + band,
            )
            hl.setColorAt(0.0, QColor(255, 255, 255, 26))
            hl.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(
                QRectF(rect.left(), rect.top(), rect.width(), band),
                QBrush(hl),
            )
            painter.restore()

        # 3) 描边：悬停时更亮
        if self._hover_highlight and self._hovered:
            border = QColor(165, 190, 255, 110)
            width = 1.3
        else:
            border = QColor(150, 172, 255, self._border_alpha)
            width = 1.0
        painter.setPen(QPen(border, width))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_blur()

    # ── 悬停 ──────────────────────────────────────────────

    def enterEvent(self, event):
        if self._hover_highlight:
            self._hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._hover_highlight:
            self._hovered = False
            self.update()
        super().leaveEvent(event)

    # ── 工具 ──────────────────────────────────────────────

    @staticmethod
    def create_qss(
        *,
        opacity: float = 0.58,
        bg_r: int = 30,
        bg_g: int = 36,
        bg_b: int = 68,
        border_alpha: float = 0.22,
        radius: int = 22,
        hover_border_alpha: float = 0.42,
    ) -> str:
        """给普通 QWidget 用的玻璃 QSS（不含阴影）。"""
        a = int(opacity * 255)
        return (
            f"background: rgba({bg_r}, {bg_g}, {bg_b}, {a});"
            f"border: 1px solid rgba(150, 172, 255, {int(border_alpha * 255)});"
            f"border-radius: {radius}px;"
        )
