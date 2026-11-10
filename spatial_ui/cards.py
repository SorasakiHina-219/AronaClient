"""
spatial_ui.cards — 空间信息卡片组件。

组成参考图中右侧信息列与舞台悬浮卡片：
  - InfoCard        通用玻璃信息卡（标题 + 右侧操作 + 内容区）
  - QuickActionsCard 快速操作（四个真实可用入口）
  - SystemStatusCard 系统状态（psutil 实时 CPU / 内存 / 磁盘）
  - ScheduleCard     今日安排（当前未接入 -> 明确标注，不虚构数据）
  - ModelInfoCard    当前模型信息（真实 AIClient 状态）
  - GreetingBubble   Arona 问候气泡

所有数据均来自真实接口；未接入的功能明确标注「未接入」。
"""

import warnings
warnings.filterwarnings("ignore")

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QProgressBar,
    QSizePolicy,
)

from spatial_ui.glass_panel import GlassPanel
from spatial_ui.styles import (
    CARD_RADIUS,
    COLOR_ACCENT_BLUE,
    COLOR_ACCENT_PINK,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_DIM,
    COLOR_SUCCESS,
    COLOR_WARNING,
    COLOR_ERROR,
    COLOR_INSET,
)


class InfoCard(GlassPanel):
    """通用玻璃信息卡。"""

    def __init__(
        self,
        title: str = "",
        *,
        parent=None,
        performance_mode: str = "medium",
        radius: int = CARD_RADIUS,
    ):
        super().__init__(
            parent,
            radius=radius,
            tint=(36, 43, 80),
            fill_alpha=196,
            performance_mode=performance_mode,
            shadow_enabled=True,
        )
        self.setObjectName("infoCard")

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(16, 13, 16, 14)
        self._root.setSpacing(9)

        self._header = QHBoxLayout()
        self._header.setContentsMargins(0, 0, 0, 0)
        self._header.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.title_label.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 14px; "
            f"font-weight: 700; background: transparent;"
        )
        self._header.addWidget(self.title_label)
        self._header.addStretch()

        self._root.addLayout(self._header)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        self._root.addLayout(self.body)

    def add_header_button(self, text: str, callback: Callable) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QToolButton {{ color: {COLOR_TEXT_MUTED}; font-size: 12px;"
            f" background: transparent; border: none; padding: 2px 4px; }}"
            f"QToolButton:hover {{ color: {COLOR_ACCENT_BLUE}; }}"
        )
        btn.clicked.connect(callback)
        self._header.addWidget(btn)
        return btn


# ──────────────────────────────────────────────────────────

class QuickActionsCard(InfoCard):
    """快速操作：四项均绑定真实功能。"""

    def __init__(
        self,
        actions: List[Tuple[str, str, Optional[Callable], bool]],
        *,
        parent=None,
        performance_mode: str = "medium",
    ):
        super().__init__(
            "快速操作", parent=parent, performance_mode=performance_mode
        )
        self.setObjectName("quickActionsCard")

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        for i, (icon, label, callback, enabled) in enumerate(actions):
            cell = self._make_cell(icon, label, callback, enabled)
            grid.addWidget(cell, i // 4, i % 4)

        self.body.addStretch(1)
        self.body.addLayout(grid)
        self.body.addStretch(1)

    def _make_cell(self, icon: str, label: str,
                   callback: Optional[Callable], enabled: bool) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(5)
        lay.setAlignment(Qt.AlignCenter)

        btn = QToolButton()
        btn.setText(icon)
        btn.setCursor(
            Qt.PointingHandCursor if (enabled and callback)
            else Qt.ArrowCursor
        )
        btn.setFixedSize(42, 42)
        btn.setEnabled(bool(enabled and callback))
        if callback and enabled:
            btn.clicked.connect(lambda _=False, cb=callback: cb())
        btn.setStyleSheet(
            f"QToolButton {{ background: rgba(123,147,255,0.16);"
            f" border: 1px solid rgba(150,172,255,0.22);"
            f" border-radius: 21px; font-size: 17px;"
            f" color: {COLOR_TEXT_PRIMARY}; }}"
            f"QToolButton:hover {{ background: rgba(123,147,255,0.30);"
            f" border-color: rgba(160,185,255,0.55); }}"
            f"QToolButton:disabled {{ background: rgba(255,255,255,0.04);"
            f" border-color: rgba(255,255,255,0.06);"
            f" color: {COLOR_TEXT_DIM}; }}"
        )
        lay.addWidget(btn, alignment=Qt.AlignHCenter)

        text = QLabel(label)
        text.setAlignment(Qt.AlignCenter)
        text.setWordWrap(True)
        text.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY if enabled else COLOR_TEXT_DIM};"
            f" font-size: 11px; background: transparent;"
        )
        lay.addWidget(text)
        return w


# ──────────────────────────────────────────────────────────

class SystemStatusCard(InfoCard):
    """系统状态：psutil 实时数据（无 psutil 时明确显示不可用）。"""

    def __init__(self, *, parent=None, performance_mode: str = "medium"):
        super().__init__(
            "系统状态", parent=parent, performance_mode=performance_mode
        )
        self.setObjectName("systemStatusCard")

        self._rows = {}
        for key, label in (("cpu", "CPU"), ("mem", "内存"), ("disk", "磁盘")):
            row = QHBoxLayout()
            row.setSpacing(8)

            name = QLabel(label)
            name.setFixedWidth(34)
            name.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
                f" background: transparent;"
            )

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(5)
            bar.setStyleSheet(
                "QProgressBar { background: rgba(140,160,235,0.16);"
                " border: none; border-radius: 3px; }"
                f"QProgressBar::chunk {{ background: {COLOR_ACCENT_BLUE};"
                " border-radius: 3px; }"
            )

            val = QLabel("--")
            val.setFixedWidth(38)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font-size: 11px;"
                f" background: transparent;"
            )

            row.addWidget(name)
            row.addWidget(bar, 1)
            row.addWidget(val)
            self.body.addLayout(row)
            self._rows[key] = (bar, val)

        self._available = True
        try:
            import psutil  # noqa: F401
        except Exception:
            self._available = False
            for bar, val in self._rows.values():
                bar.setVisible(False)
                val.setText("不可用")
            note = QLabel("未安装 psutil，系统状态不可用")
            note.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 10px;"
                f" background: transparent;"
            )
            self.body.addWidget(note)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def start_monitoring(self, interval_ms: int = 3000):
        if self._available and not self._timer.isActive():
            self._timer.start(interval_ms)

    def stop_monitoring(self):
        if self._timer.isActive():
            self._timer.stop()

    def refresh(self):
        if not self._available:
            return
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            try:
                disk = psutil.disk_usage("C:\\").percent
            except Exception:
                disk = psutil.disk_usage("/").percent
            for key, value in (("cpu", cpu), ("mem", mem), ("disk", disk)):
                bar, val = self._rows[key]
                bar.setValue(int(value))
                val.setText(f"{int(value)}%")
                if value >= 88:
                    color = COLOR_ERROR
                elif value >= 70:
                    color = COLOR_WARNING
                else:
                    color = COLOR_ACCENT_BLUE
                bar.setStyleSheet(
                    "QProgressBar { background: rgba(140,160,235,0.16);"
                    " border: none; border-radius: 3px; }"
                    f"QProgressBar::chunk {{ background: {color};"
                    " border-radius: 3px; }"
                )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────

class ScheduleCard(InfoCard):
    """今日安排 —— 日程模块尚未接入，明确标注，不展示虚构数据。"""

    def __init__(self, *, parent=None, performance_mode: str = "medium"):
        super().__init__(
            "今日安排", parent=parent, performance_mode=performance_mode
        )
        self.setObjectName("scheduleCard")

        box = QWidget()
        box.setStyleSheet(
            "background: rgba(255,255,255,0.035);"
            " border: 1px dashed rgba(150,172,255,0.22);"
            " border-radius: 10px;"
        )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(4)

        main = QLabel("日程模块尚未接入")
        main.setAlignment(Qt.AlignCenter)
        main.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: 12px;"
            f" background: transparent; border: none;"
        )

        sub = QLabel("接入后将在此显示提醒与任务")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 10px;"
            f" background: transparent; border: none;"
        )

        lay.addWidget(main)
        lay.addWidget(sub)
        self.body.addStretch(1)
        self.body.addWidget(box)
        self.body.addStretch(1)


# ──────────────────────────────────────────────────────────

class ModelInfoCard(InfoCard):
    """当前模型信息（真实数据 + 跳转设置）。"""

    switch_requested = Signal()

    def __init__(self, *, parent=None, performance_mode: str = "medium"):
        super().__init__(
            "模型", parent=parent, performance_mode=performance_mode
        )
        self.setObjectName("modelInfoCard")

        row = QHBoxLayout()
        row.setSpacing(10)

        self.avatar = QLabel("A")
        self.avatar.setAlignment(Qt.AlignCenter)
        self.avatar.setFixedSize(38, 38)
        self.avatar.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #7b93ff, stop:1 #a78bfa);"
            " border-radius: 19px; color: #0e1230;"
            " font-weight: 800; font-size: 15px; border: none;"
        )

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        self.name_label = QLabel("Arona")
        self.name_label.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 13px;"
            f" font-weight: 700; background: transparent;"
        )

        self.model_label = QLabel("未配置")
        self.model_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )

        self.status_label = QLabel("● 未配置")
        self.status_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )

        text_col.addWidget(self.name_label)
        text_col.addWidget(self.model_label)
        text_col.addWidget(self.status_label)

        row.addWidget(self.avatar)
        row.addLayout(text_col, 1)

        self.switch_btn = QPushButton("切换")
        self.switch_btn.setCursor(Qt.PointingHandCursor)
        self.switch_btn.setFixedHeight(28)
        self.switch_btn.setStyleSheet(
            f"QPushButton {{ background: rgba(255,255,255,0.08);"
            f" border: 1px solid rgba(160,185,255,0.28);"
            f" border-radius: 14px; padding: 0 14px;"
            f" color: {COLOR_TEXT_SECONDARY}; font-size: 11px; }}"
            f"QPushButton:hover {{ background: rgba(123,147,255,0.26);"
            f" color: {COLOR_TEXT_PRIMARY}; }}"
        )
        self.switch_btn.clicked.connect(self.switch_requested.emit)
        row.addWidget(self.switch_btn)

        self.body.addStretch(1)
        self.body.addLayout(row)
        self.body.addStretch(1)

    def set_model_info(self, provider: str, model: str, connected: bool):
        self.model_label.setText(f"{provider} · {model}")
        if connected:
            self.status_label.setText("● 已连接")
            self.status_label.setStyleSheet(
                f"color: {COLOR_SUCCESS}; font-size: 11px;"
                f" background: transparent;"
            )
        else:
            self.status_label.setText("● 未配置")
            self.status_label.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
                f" background: transparent;"
            )


# ──────────────────────────────────────────────────────────

class GreetingBubble(GlassPanel):
    """Arona 问候气泡（悬浮在舞台左下）。"""

    def __init__(self, *, parent=None, performance_mode: str = "medium"):
        super().__init__(
            parent,
            radius=16,
            tint=(62, 48, 96),
            fill_alpha=210,
            performance_mode=performance_mode,
            shadow_enabled=True,
        )
        self.setObjectName("greetingBubble")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 14, 10)
        lay.setSpacing(10)

        icon = QLabel("♡")
        icon.setFixedSize(28, 28)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f"background: rgba(240,166,200,0.22);"
            f" border: 1px solid rgba(240,166,200,0.45);"
            f" border-radius: 14px; color: {COLOR_ACCENT_PINK};"
            f" font-size: 13px;"
        )

        col = QVBoxLayout()
        col.setSpacing(2)

        self.title_label = QLabel("晚上好，主人～")
        self.title_label.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 13px;"
            f" font-weight: 700; background: transparent;"
        )

        self.sub_label = QLabel("今天也要加油哦！♡")
        self.sub_label.setStyleSheet(
            f"color: {COLOR_ACCENT_PINK}; font-size: 11px;"
            f" background: transparent;"
        )

        col.addWidget(self.title_label)
        col.addWidget(self.sub_label)

        lay.addWidget(icon)
        lay.addLayout(col)

    def set_greeting(self, title: str, sub: str):
        self.title_label.setText(title)
        self.sub_label.setText(sub)
