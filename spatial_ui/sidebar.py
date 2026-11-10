"""
spatial_ui.sidebar — 空间侧边栏（导航列）。

信息层级（参考 visionOS + 夜间樱色空间）：
  1. Arona 档案卡：头像 / 名称 / 在线状态 / 一句问候
  2. 主导航：首页 / 聊天 / 项目 / 日历 / 模型
  3. 分隔
  4. 设置
  5. 底部：Arona OS 版本号

收起态（76px）：仅保留头像与图标，Tooltip 提供文字。
"""

import warnings
warnings.filterwarnings("ignore")

from typing import List, Tuple

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QFrame,
    QButtonGroup,
)

from spatial_ui.glass_panel import GlassPanel
from spatial_ui.styles import (
    SIDEBAR_EXPANDED_WIDTH,
    SIDEBAR_COLLAPSED_WIDTH,
    COLOR_ACCENT_BLUE,
    COLOR_ACCENT_PINK,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_DIM,
    COLOR_SUCCESS,
    PANEL_RADIUS,
)


class _NavItem(QToolButton):
    """单个导航项：图标 + 文字，支持选中态。"""

    def __init__(self, icon: str, label: str, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._icon = icon
        self._label = label
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setText(f"{icon}   {label}")
        self.setToolTip(label)
        self.setMinimumHeight(38)
        self.setStyleSheet(self._qss(False))
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked: bool):
        self.setStyleSheet(self._qss(checked))

    def _qss(self, checked: bool) -> str:
        if checked:
            return (
                "QToolButton {"
                " background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                " stop:0 rgba(123,147,255,0.38),"
                " stop:1 rgba(167,139,250,0.24));"
                " border: 1px solid rgba(150,172,255,0.45);"
                " border-radius: 12px;"
                " padding: 8px 12px;"
                f" color: {COLOR_TEXT_PRIMARY}; font-size: 13px;"
                " font-weight: 600; text-align: left;"
                "}"
            )
        return (
            "QToolButton {"
            " background: transparent; border: 1px solid transparent;"
            " border-radius: 12px; padding: 8px 12px;"
            f" color: {COLOR_TEXT_MUTED}; font-size: 13px;"
            " text-align: left;"
            "}"
            "QToolButton:hover {"
            " background: rgba(255,255,255,0.06);"
            f" color: {COLOR_TEXT_SECONDARY};"
            "}"
        )

    def set_collapsed(self, collapsed: bool):
        self.setText(self._icon if collapsed else f"{self._icon}   {self._label}")


class SpatialSidebar(GlassPanel):
    """空间侧边栏。"""

    new_chat_requested = Signal()
    page_selected = Signal(str)          # home / chat / project / calendar / model
    settings_clicked = Signal()
    collapsed_changed = Signal(bool)

    NAV_ITEMS: List[Tuple[str, str, str]] = [
        ("⌂", "首页", "home"),
        ("✉", "聊天", "chat"),
        ("▣", "项目", "project"),
        ("◈", "日历", "calendar"),
        ("◐", "模型", "model"),
    ]

    def __init__(
        self,
        parent=None,
        *,
        performance_mode: str = "medium",
    ):
        super().__init__(
            parent,
            radius=PANEL_RADIUS,
            tint=(34, 41, 78),
            fill_alpha=202,
            performance_mode=performance_mode,
            shadow_enabled=True,
        )

        self._collapsed = False
        self._perf_mode = performance_mode
        self._nav_items: List[_NavItem] = []

        self.setObjectName("spatialSidebar")
        self.setFixedWidth(SIDEBAR_EXPANDED_WIDTH)

        self._build_ui()

    # ── 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 14)
        root.setSpacing(10)

        # ── 1. Arona 档案卡 ────────────────────────
        self.profile = QWidget()
        self.profile.setObjectName("sidebarProfile")
        pl = QHBoxLayout(self.profile)
        pl.setContentsMargins(4, 2, 4, 2)
        pl.setSpacing(10)

        self.avatar = QLabel("A")
        self.avatar.setAlignment(Qt.AlignCenter)
        self.avatar.setFixedSize(42, 42)
        self.avatar.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #8ea6ff, stop:1 #b39bff);"
            " border: 1px solid rgba(220,230,255,0.55);"
            " border-radius: 21px; color: #10142e;"
            " font-weight: 800; font-size: 17px;"
        )

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        text_col.setContentsMargins(0, 0, 0, 0)

        name_row = QHBoxLayout()
        name_row.setSpacing(5)
        name_row.setContentsMargins(0, 0, 0, 0)

        self.name_label = QLabel("Arona")
        self.name_label.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 14px;"
            f" font-weight: 700; background: transparent;"
        )

        self.online_dot = QLabel("●")
        self.online_dot.setStyleSheet(
            f"color: {COLOR_SUCCESS}; font-size: 9px;"
            f" background: transparent;"
        )
        name_row.addWidget(self.name_label)
        name_row.addWidget(self.online_dot)
        name_row.addStretch()

        self.greeting_label = QLabel("「……有新的任务吗？」")
        self.greeting_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )

        text_col.addLayout(name_row)
        text_col.addWidget(self.greeting_label)

        self.new_chat_btn = QToolButton()
        self.new_chat_btn.setText("＋")
        self.new_chat_btn.setCursor(Qt.PointingHandCursor)
        self.new_chat_btn.setFixedSize(28, 28)
        self.new_chat_btn.setToolTip("新建对话")
        self.new_chat_btn.setStyleSheet(
            "QToolButton { background: rgba(123,147,255,0.20);"
            " border: 1px solid rgba(150,172,255,0.35);"
            " border-radius: 14px; color: #dfe6ff; font-size: 14px; }"
            "QToolButton:hover { background: rgba(123,147,255,0.40); }"
        )
        self.new_chat_btn.clicked.connect(self.new_chat_requested.emit)

        pl.addWidget(self.avatar)
        pl.addLayout(text_col, 1)
        pl.addWidget(self.new_chat_btn, 0, Qt.AlignTop)

        root.addWidget(self.profile)

        sep0 = QFrame()
        sep0.setFixedHeight(1)
        sep0.setStyleSheet("background: rgba(150,172,255,0.14);")
        root.addWidget(sep0)

        # ── 2. 主导航 ──────────────────────────────
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        nav_wrap = QVBoxLayout()
        nav_wrap.setContentsMargins(0, 2, 0, 2)
        nav_wrap.setSpacing(4)

        for icon, label, key in self.NAV_ITEMS:
            item = _NavItem(icon, label, key)
            item.clicked.connect(lambda _c=False, k=key: self._on_nav(k))
            self._button_group.addButton(item)
            self._nav_items.append(item)
            nav_wrap.addWidget(item)

        root.addLayout(nav_wrap)

        # 默认选中首页
        self._nav_items[0].setChecked(True)

        # ── 3. 分隔 ────────────────────────────────
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(150,172,255,0.14);")
        root.addWidget(sep)

        # ── 4. 设置 ────────────────────────────────
        self.settings_btn = _NavItem("⚙", "设置", "settings")
        self.settings_btn.setCheckable(False)
        self.settings_btn.clicked.connect(self.settings_clicked.emit)
        root.addWidget(self.settings_btn)

        root.addStretch(1)

        # ── 5. 底部版本 ────────────────────────────
        self.version_box = QWidget()
        self.version_box.setStyleSheet(
            "background: rgba(255,255,255,0.045);"
            " border: 1px solid rgba(150,172,255,0.14);"
            " border-radius: 12px;"
        )
        vl = QHBoxLayout(self.version_box)
        vl.setContentsMargins(10, 8, 10, 8)
        vl.setSpacing(8)

        os_icon = QLabel("❀")
        os_icon.setStyleSheet(
            f"color: {COLOR_ACCENT_PINK}; font-size: 15px;"
            f" background: transparent; border: none;"
        )

        vcol = QVBoxLayout()
        vcol.setSpacing(0)
        vcol.setContentsMargins(0, 0, 0, 0)

        os_name = QLabel("Arona OS")
        os_name.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: 12px;"
            f" font-weight: 600; background: transparent; border: none;"
        )
        os_ver = QLabel("v0.2.0 · 空间界面")
        os_ver.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 10px;"
            f" background: transparent; border: none;"
        )

        vcol.addWidget(os_name)
        vcol.addWidget(os_ver)

        vl.addWidget(os_icon)
        vl.addLayout(vcol, 1)

        root.addWidget(self.version_box)

    # ── 行为 ──────────────────────────────────────────────

    def _on_nav(self, key: str):
        self.page_selected.emit(key)

    def select_page(self, key: str):
        """外部同步选中的导航项。"""
        for item in self._nav_items:
            if item.key == key:
                item.setChecked(True)
                break

    def set_collapsed(self, collapsed: bool):
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(
            SIDEBAR_COLLAPSED_WIDTH if collapsed
            else SIDEBAR_EXPANDED_WIDTH
        )

        for item in self._nav_items:
            item.set_collapsed(collapsed)
        self.settings_btn.set_collapsed(collapsed)

        self.name_label.setVisible(not collapsed)
        self.online_dot.setVisible(not collapsed)
        self.greeting_label.setVisible(not collapsed)
        self.new_chat_btn.setVisible(not collapsed)
        self.version_box.setVisible(not collapsed)

        # 收起时头像居中
        self.profile.layout().setContentsMargins(
            4 if collapsed else 4, 2, 4, 2
        )

        self.collapsed_changed.emit(collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    def set_greeting(self, text: str):
        self.greeting_label.setText(text)

    def set_avatar_text(self, text: str):
        self.avatar.setText(text)

    # ── 兼容旧接口 ────────────────────────────────────────

    def add_history_item(self, *args, **kwargs):
        """旧接口保留（历史列表已迁移到聊天页）。"""
        return None

    def clear_history(self):
        return None

    def set_model_info(self, provider: str, model: str, connected: bool):
        """旧接口保留（模型状态卡片已迁移到右侧信息列）。"""
        return None

    def set_performance_mode(self, mode: str):
        self._perf_mode = mode
        super().set_performance_mode(mode)
