"""
spatial_ui.chat_fade — 聊天消息内容区的滚动渐隐。

只作用于消息内容区域：
  - 顶部：文字向上滚动时进入顶部渐变带逐渐淡出（滚动到最顶部时自动减弱）；
  - 底部：文字离开可视区前逐渐淡出。

实现：在 QScrollArea.viewport() 上覆盖一个**透明且不接收鼠标**的子控件，
paintEvent 里画上下两条 alpha 渐变。消息本身仍由 Qt 正常滚动，
不改变滚动条行为、不改变聊天面板尺寸与位置。
"""

import warnings
warnings.filterwarnings("ignore")

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QPainter, QColor, QLinearGradient
from PySide6.QtWidgets import QWidget

from spatial_ui.styles import FADE_TOP_HEIGHT, FADE_BOTTOM_HEIGHT


class FadeOverlay(QWidget):
    """聊天内容区上下渐隐遮罩（不拦截鼠标、不参与布局）。"""

    def __init__(
        self,
        viewport: QWidget,
        scroll_area,
        *,
        color=(30, 37, 70),
        top_height: int = FADE_TOP_HEIGHT,
        bottom_height: int = FADE_BOTTOM_HEIGHT,
        parent=None,
    ):
        super().__init__(viewport if parent is None else parent)

        self._viewport = viewport
        self._scroll = scroll_area
        self._color = QColor(*color)
        self._top_h = int(top_height)
        self._bottom_h = int(bottom_height)

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        viewport.installEventFilter(self)
        try:
            scroll_area.verticalScrollBar().valueChanged.connect(self.update)
        except Exception:
            pass
        self._sync_geometry()

    # ── 几何 ──────────────────────────────────────────────

    def _sync_geometry(self):
        try:
            self.setGeometry(0, 0, self._viewport.width(), self._viewport.height())
        except Exception:
            pass

    def eventFilter(self, obj, event):
        if obj is self._viewport and event.type() in (QEvent.Resize, QEvent.Show):
            self._sync_geometry()
        return False

    def set_color(self, color):
        self._color = QColor(*color)
        self.update()

    def set_performance_mode(self, mode: str):
        # low 模式：渐变带更短更省（绘制成本本来就极低）
        if mode == "low":
            self._top_h = max(36, int(FADE_TOP_HEIGHT * 0.7))
        else:
            self._top_h = FADE_TOP_HEIGHT
        self.update()

    # ── 绘制 ──────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            p.end()
            return

        c = self._color
        base_alpha = 236

        # 顶部：滚到最顶端时淡出带自然减弱（避免首条消息被压暗）
        value = 0
        maximum = 0
        try:
            bar = self._scroll.verticalScrollBar()
            value = bar.value()
            maximum = bar.maximum()
        except Exception:
            pass

        top_strength = 1.0
        if value <= 0:
            top_strength = 0.0
        elif value < 40:
            top_strength = value / 40.0

        if top_strength > 0.01:
            th = self._top_h
            g = QLinearGradient(0, 0, 0, th)
            a0 = int(base_alpha * top_strength)
            g.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), a0))
            g.setColorAt(0.55, QColor(c.red(), c.green(), c.blue(), int(a0 * 0.45)))
            g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            p.fillRect(0, 0, w, th, g)

        # 底部：始终保留一条柔和淡出（提醒还有更多内容）
        if maximum > 0 and value < maximum:
            bh = self._bottom_h
            g2 = QLinearGradient(0, h - bh, 0, h)
            g2.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 0))
            g2.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), int(base_alpha * 0.85)))
            p.fillRect(0, h - bh, w, bh, g2)

        p.end()
