"""
spatial_ui.floating_menu — 悬浮快捷菜单（两级）。

A. 常驻快捷栏 QuickBar
   - 胶囊式玻璃容器，图标优先，悬停显示文字提示，当前入口柔和高亮。
   - 位置由宿主决定（默认贴在中央区域右侧垂直居中，不遮挡输入框与模型主体）。

B. 上下文菜单 ContextPanel
   - 点击入口后在入口附近展开；
   - 根据屏幕边缘自动翻转/夹取位置；
   - 点击其他入口会切换内容；
   - 点空白或按 Esc 收起（Qt.Popup 自带的点击外部关闭 + 自行处理 Esc）。

所有条目都绑定真实动作（由宿主传入），不虚构功能。
"""

import warnings
warnings.filterwarnings("ignore")

from typing import Iterable, List, Optional, Tuple

from PySide6.QtCore import Qt, QPoint, QRect, Signal
from PySide6.QtGui import QGuiApplication, QColor, QPainter, QPen, QLinearGradient, QBrush
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QToolButton,
    QLabel,
)

from spatial_ui.glass_panel import GlassPanel
from spatial_ui.styles import (
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_ACCENT_BLUE,
    COLOR_SUCCESS,
)


class _EntryButton(QToolButton):
    """快捷栏中的单个入口按钮。"""

    def __init__(self, key: str, icon: str, tip: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._icon = icon
        self.setText(icon)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)
        self.setFixedSize(38, 38)
        self.setStyleSheet(self._qss(False))

    def _qss(self, active: bool) -> str:
        if active:
            return (
                "QToolButton {"
                " background: rgba(123,147,255,0.38);"
                " border: 1px solid rgba(170,195,255,0.60);"
                " border-radius: 19px;"
                f" color: {COLOR_TEXT_PRIMARY}; font-size: 16px;"
                "}"
            )
        return (
            "QToolButton {"
            " background: transparent; border: 1px solid transparent;"
            " border-radius: 19px;"
            f" color: {COLOR_TEXT_SECONDARY}; font-size: 16px;"
            "}"
            "QToolButton:hover {"
            " background: rgba(255,255,255,0.10);"
            f" color: {COLOR_TEXT_PRIMARY};"
            "}"
        )

    def set_active(self, active: bool):
        self.setChecked(active)
        self.setStyleSheet(self._qss(active))


class QuickBar(GlassPanel):
    """常驻悬浮快捷栏（胶囊式）。"""

    entry_clicked = Signal(str)
    entry_context = Signal(str, QPoint)     # key, 全局坐标（按钮下方）

    def __init__(
        self,
        parent=None,
        *,
        entries: Iterable[Tuple[str, str, str]] = (),
        performance_mode: str = "medium",
        orientation: str = "vertical",
        blur_provider=None,
    ):
        super().__init__(
            parent,
            kind="quick",
            tint=(34, 41, 78),
            performance_mode=performance_mode,
            blur_provider=blur_provider,
        )
        self.setObjectName("spatialQuickBar")
        self._buttons = {}
        self._active = ""

        if orientation == "vertical":
            lay = QVBoxLayout(self)
        else:
            lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 7, 7, 7)
        lay.setSpacing(5)

        for key, icon, tip in entries:
            btn = _EntryButton(key, icon, tip)
            btn.clicked.connect(lambda _c=False, k=key: self._on_click(k))
            lay.addWidget(btn, alignment=Qt.AlignCenter)
            self._buttons[key] = btn

        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    # ── 交互 ──────────────────────────────────────────────

    def _on_click(self, key: str):
        # 再次点击同一入口 = 只切换上下文菜单（由宿主决定是否重复展开）
        self.set_active(key)
        self.entry_clicked.emit(key)
        btn = self._buttons.get(key)
        if btn is not None:
            self.entry_context.emit(key, btn.mapToGlobal(btn.rect().bottomLeft()))

    def set_active(self, key: str):
        self._active = key
        for k, b in self._buttons.items():
            b.set_active(k == key)

    def active_key(self) -> str:
        return self._active

    def entry_rect_global(self, key: str) -> QRect:
        btn = self._buttons.get(key)
        if btn is None:
            return QRect()
        tl = btn.mapToGlobal(btn.rect().topLeft())
        return QRect(tl, btn.size())


class ContextPanel(GlassPanel):
    """上下文菜单（Qt.Popup，点击外部自动关闭）。"""

    action_triggered = Signal(str)

    def __init__(self, parent=None, *, performance_mode: str = "medium"):
        super().__init__(
            parent,
            kind="quick",
            tint=(36, 43, 82),
            performance_mode=performance_mode,
            shadow_enabled=True,
        )
        self.setObjectName("spatialContextMenu")
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(8, 8, 8, 8)
        self._lay.setSpacing(2)
        self._items = []

    # ── 内容 ──────────────────────────────────────────────

    def set_items(self, title: str, items: List[Tuple[str, str, str, bool]]):
        """items: [(key, icon, text, enabled), ...]"""
        while self._lay.count():
            w = self._lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._items = []

        head = QLabel(title)
        head.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent; padding: 4px 8px 6px 8px;"
        )
        self._lay.addWidget(head)

        for key, icon, text, enabled in items:
            btn = QToolButton()
            btn.setObjectName("contextItem")
            btn.setText(f"{icon}   {text}")
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
            btn.setEnabled(enabled)
            btn.setFixedHeight(32)
            btn.setStyleSheet(
                "QToolButton#contextItem {"
                " background: transparent; border: 1px solid transparent;"
                " border-radius: 9px; padding: 0 12px; text-align: left;"
                f" color: {COLOR_TEXT_SECONDARY}; font-size: 12px;"
                "}"
                "QToolButton#contextItem:hover {"
                " background: rgba(123,147,255,0.24);"
                f" color: {COLOR_TEXT_PRIMARY};"
                "}"
                "QToolButton#contextItem:disabled {"
                f" color: {COLOR_TEXT_MUTED};"
                "}"
            )
            btn.clicked.connect(
                lambda _c=False, k=key: self._on_action(k)
            )
            self._lay.addWidget(btn)
            self._items.append(key)

        self.adjustSize()
        self.setFixedWidth(max(196, self.sizeHint().width()))

    def _on_action(self, key: str):
        self.action_triggered.emit(key)
        self.close()

    # ── 定位 ──────────────────────────────────────────────

    def open_near(self, anchor: QRect):
        """在锚点旁边展开；靠近屏幕边缘时自动翻转/夹取。"""
        self.adjustSize()
        w, h = self.width(), self.height()

        screen = QGuiApplication.screenAt(anchor.center())
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

        # 优先放在按钮右侧；右侧空间不足则放左侧
        x = anchor.right() + 8
        if x + w > avail.right() - 6:
            x = anchor.left() - w - 8
        # 垂直居中对齐按钮，越界则夹取
        y = anchor.center().y() - h // 2
        y = max(avail.top() + 6, min(y, avail.bottom() - h - 6))
        x = max(avail.left() + 6, min(x, avail.right() - w - 6))

        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    # ── Esc 关闭 ──────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        # Qt.Popup 是独立顶层窗口，这里直接复用玻璃面板绘制
        super().paintEvent(event)
