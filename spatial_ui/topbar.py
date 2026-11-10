"""
spatial_ui.topbar — 空间顶栏。

布局（对应参考图）：
  左：AronaClient 标志
  中：导航胶囊（主页 / 聊天 / 项目 / 文件 / 日历 / 模型 / ＋）
  右：实时时钟 + 连接状态 + 设置 + 头像

数据全部真实：时钟来自系统时间，连接状态来自 AIClient.is_configured()。
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
from typing import List, Tuple

from PySide6.QtCore import Qt, Signal, QTimer, QPointF
from PySide6.QtGui import QPainter, QLinearGradient, QColor, QPen
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QButtonGroup,
    QFrame,
)

from spatial_ui.styles import (
    TOPBAR_HEIGHT,
    COLOR_ACCENT_BLUE,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_SUCCESS,
    COLOR_WARNING,
)

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class _NavPill(QToolButton):
    """顶部导航胶囊。"""

    def __init__(self, icon: str, label: str, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._icon = icon
        self._label = label
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setText(f"{icon}  {label}")
        self.setMinimumHeight(32)
        self.setStyleSheet(self._qss(False))
        self.toggled.connect(lambda c: self.setStyleSheet(self._qss(c)))

    def _qss(self, checked: bool) -> str:
        if checked:
            return (
                "QToolButton {"
                " background: rgba(123,147,255,0.30);"
                " border: 1px solid rgba(160,185,255,0.50);"
                " border-radius: 16px; padding: 5px 14px;"
                f" color: {COLOR_TEXT_PRIMARY}; font-size: 12px;"
                " font-weight: 600;"
                "}"
            )
        return (
            "QToolButton {"
            " background: transparent; border: 1px solid transparent;"
            " border-radius: 16px; padding: 5px 14px;"
            f" color: {COLOR_TEXT_MUTED}; font-size: 12px;"
            "}"
            "QToolButton:hover {"
            " background: rgba(255,255,255,0.07);"
            f" color: {COLOR_TEXT_SECONDARY};"
            "}"
        )


class SpatialTopbar(QWidget):
    """空间顶栏。"""

    sidebar_toggle = Signal()
    page_selected = Signal(str)
    settings_clicked = Signal()
    new_chat_requested = Signal()

    PILLS: List[Tuple[str, str, str]] = [
        ("⌂", "主页", "home"),
        ("✉", "聊天", "chat"),
        ("▣", "项目", "project"),
        ("▤", "文件", "file"),
        ("◈", "日历", "calendar"),
        ("◐", "模型", "model"),
    ]

    def __init__(self, parent=None, *, performance_mode: str = "medium"):
        super().__init__(parent)
        self._perf_mode = performance_mode
        self._pills: List[_NavPill] = []

        self.setObjectName("spatialTopbar")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setFixedHeight(TOPBAR_HEIGHT)

        self._build_ui()
        self._start_clock()

    # ── 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 10, 22, 10)
        lay.setSpacing(14)

        # ── 左：Logo ───────────────────────────────
        self.logo = QLabel("A")
        self.logo.setAlignment(Qt.AlignCenter)
        self.logo.setFixedSize(30, 30)
        self.logo.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #8ea6ff, stop:1 #b39bff);"
            " border-radius: 15px; color: #0f1330;"
            " font-weight: 900; font-size: 15px;"
        )

        brand = QLabel("AronaClient")
        brand.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 15px;"
            f" font-weight: 700; background: transparent;"
        )

        lay.addWidget(self.logo)
        lay.addWidget(brand)

        # ── 中：导航胶囊 ───────────────────────────
        lay.addStretch(1)

        pill_wrap = QWidget()
        pill_lay = QHBoxLayout(pill_wrap)
        pill_lay.setContentsMargins(0, 0, 0, 0)
        pill_lay.setSpacing(4)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        for icon, label, key in self.PILLS:
            pill = _NavPill(icon, label, key)
            pill.clicked.connect(lambda _c=False, k=key: self._on_pill(k))
            self._group.addButton(pill)
            self._pills.append(pill)
            pill_lay.addWidget(pill)

        self.plus_pill = QToolButton()
        self.plus_pill.setText("＋")
        self.plus_pill.setCursor(Qt.PointingHandCursor)
        self.plus_pill.setToolTip("新建对话")
        self.plus_pill.setFixedSize(32, 32)
        self.plus_pill.setStyleSheet(
            "QToolButton { background: rgba(255,255,255,0.07);"
            " border: 1px solid rgba(150,172,255,0.22);"
            " border-radius: 16px; color: #c9d2f5; font-size: 14px; }"
            "QToolButton:hover { background: rgba(123,147,255,0.30);"
            " border-color: rgba(160,185,255,0.5); color: #eef1ff; }"
        )
        self.plus_pill.clicked.connect(self.new_chat_requested.emit)
        pill_lay.addWidget(self.plus_pill)

        lay.addWidget(pill_wrap)
        lay.addStretch(1)

        # ── 右：时钟 / 状态 / 图标 ──────────────────
        right = QWidget()
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)

        self.clock_icon = QLabel("◷")
        self.clock_icon.setStyleSheet(
            f"color: {COLOR_ACCENT_BLUE}; font-size: 15px;"
            f" background: transparent;"
        )
        rl.addWidget(self.clock_icon)

        clock_col = QWidget()
        cl = QHBoxLayout(clock_col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)

        self.time_label = QLabel("--:--")
        self.time_label.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 13px;"
            f" font-weight: 600; background: transparent;"
        )

        self.date_label = QLabel("--月--日 周-")
        self.date_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 10px;"
            f" background: transparent;"
        )

        cl.addWidget(self.time_label)
        cl.addWidget(self.date_label)
        rl.addWidget(clock_col)

        vsep = QFrame()
        vsep.setFixedSize(1, 26)
        vsep.setStyleSheet("background: rgba(150,172,255,0.16);")
        rl.addWidget(vsep)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(
            f"color: {COLOR_WARNING}; font-size: 9px;"
            f" background: transparent;"
        )
        self.status_text = QLabel("未配置")
        self.status_text.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )
        rl.addWidget(self.status_dot)
        rl.addWidget(self.status_text)

        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.setFixedSize(30, 30)
        self.settings_btn.setStyleSheet(
            "QToolButton { background: transparent; border: none;"
            f" border-radius: 15px; color: {COLOR_TEXT_SECONDARY};"
            " font-size: 15px; }"
            "QToolButton:hover { background: rgba(255,255,255,0.08);"
            " color: #eef1ff; }"
        )
        self.settings_btn.clicked.connect(self.settings_clicked.emit)
        rl.addWidget(self.settings_btn)

        self.avatar = QLabel("A")
        self.avatar.setAlignment(Qt.AlignCenter)
        self.avatar.setFixedSize(32, 32)
        self.avatar.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #8ea6ff, stop:1 #b39bff);"
            " border: 1px solid rgba(220,230,255,0.5);"
            " border-radius: 16px; color: #10142e;"
            " font-weight: 800; font-size: 13px;"
        )
        rl.addWidget(self.avatar)

        lay.addWidget(right)

    # ── 行为 ──────────────────────────────────────────────

    def _on_pill(self, key: str):
        self.page_selected.emit(key)

    def select_page(self, key: str):
        for p in self._pills:
            if p.key == key:
                p.setChecked(True)
                break

    def _start_clock(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick)
        self._clock_timer.start(1000)
        self._tick()

    def _tick(self):
        now = datetime.now()
        self.time_label.setText(now.strftime("%H:%M"))
        self.date_label.setText(
            f"{now.month}月{now.day}日 {_WEEKDAYS[now.weekday()]}"
        )

    def set_connection_state(self, connected: bool):
        if connected:
            self.status_dot.setStyleSheet(
                f"color: {COLOR_SUCCESS}; font-size: 9px;"
                f" background: transparent;"
            )
            self.status_text.setText("已连接")
            self.status_text.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font-size: 11px;"
                f" background: transparent;"
            )
        else:
            self.status_dot.setStyleSheet(
                f"color: {COLOR_WARNING}; font-size: 9px;"
                f" background: transparent;"
            )
            self.status_text.setText("未配置")
            self.status_text.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
                f" background: transparent;"
            )

    def set_subtitle(self, text: str):
        """兼容旧接口（旧版副标题已由连接状态替代）。"""
        return None

    def set_performance_mode(self, mode: str):
        self._perf_mode = mode

    # ── 绘制：顶栏底部渐变分隔 ─────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        h = self.height()

        grad = QLinearGradient(QPointF(0, 0), QPointF(0, h))
        grad.setColorAt(0.0, QColor(22, 27, 52, 120))
        grad.setColorAt(1.0, QColor(14, 18, 36, 60))
        p.fillRect(self.rect(), grad)

        p.setPen(QPen(QColor(150, 172, 255, 26), 1))
        p.drawLine(0, h - 1, self.width(), h - 1)
        p.end()
