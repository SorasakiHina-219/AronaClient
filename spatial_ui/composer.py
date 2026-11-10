"""
spatial_ui.composer — 浮动消息输入区（深色玻璃胶囊）。

视觉：对应参考图底部中央的输入胶囊 —— 半透明深色玻璃、圆角、右侧渐变发送键。
交互：
  - Enter 发送 / Shift+Enter 换行
  - 附件按钮（打开文件对话框，附件能力沿用原有接口）
  - 表情按钮（插入常用表情）
  - 麦克风按钮（触发语音输入，未接入时由主窗口提示）
数据：全部走原有 AIClient 发送链路，本组件只负责采集与发出信号。
"""

import warnings
warnings.filterwarnings("ignore")

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QTextEdit,
    QToolButton,
    QMenu,
    QSizePolicy,
)

from spatial_ui.glass_panel import GlassPanel
from spatial_ui.styles import (
    COMPOSER_HEIGHT,
    COMPOSER_MAX_HEIGHT,
    COLOR_ACCENT_BLUE,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_MUTED,
)


class _RoundBtn(QToolButton):
    """圆形工具按钮。"""

    def __init__(self, text: str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setFixedSize(34, 34)
        self.setStyleSheet(
            "QToolButton { background: transparent; border: none;"
            " border-radius: 17px;"
            f" color: {COLOR_TEXT_MUTED}; font-size: 16px; }}"
            "QToolButton:hover { background: rgba(255,255,255,0.09);"
            f" color: {COLOR_TEXT_PRIMARY}; }}"
        )


class SpatialComposer(GlassPanel):
    """浮动输入胶囊。"""

    message_sent = Signal(str)
    voice_started = Signal()
    attachment_requested = Signal()
    emoji_requested = Signal(str)

    EMOJIS = (
        "😊", "😄", "🥰", "😴", "🤔", "😮",
        "✨", "🌸", "💙", "🎀", "🌙", "⭐",
        "👍", "🙏", "🫶", "💤", "☕", "🎵",
    )

    def __init__(self, parent=None, *, performance_mode: str = "medium",
                 blur_provider=None):
        super().__init__(
            parent,
            radius=26,
            tint=(46, 54, 98),
            fill_alpha=214,
            performance_mode=performance_mode,
            shadow_enabled=True,
            blur_provider=blur_provider,
        )

        self._perf_mode = performance_mode
        self.setObjectName("spatialComposer")

        self._build_ui()
        self.setFixedHeight(COMPOSER_HEIGHT + 12)

    # ── 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # 附件
        self.attach_btn = _RoundBtn("📎", "添加附件")
        self.attach_btn.clicked.connect(self.attachment_requested.emit)
        lay.addWidget(self.attach_btn)

        # 表情
        self.emoji_btn = _RoundBtn("😊", "插入表情")
        self.emoji_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        for i in range(0, len(self.EMOJIS), 6):
            row = self.EMOJIS[i:i + 6]
            act = menu.addAction("  ".join(row))
            emoji = row[0]
            act.triggered.connect(
                lambda _c=False, e=emoji: self.emoji_requested.emit(e)
            )
        self.emoji_btn.setMenu(menu)
        lay.addWidget(self.emoji_btn)

        # 输入框
        self.input = QTextEdit()
        self.input.setObjectName("spatialMessageInput")
        self.input.setPlaceholderText("和 Arona 聊天吧…")
        self.input.setAcceptRichText(False)
        self.input.setFixedHeight(COMPOSER_HEIGHT - 16)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.input.setStyleSheet(
            "QTextEdit {"
            " background: transparent; border: none;"
            f" color: {COLOR_TEXT_PRIMARY}; font-size: 13px;"
            " padding: 4px 2px;"
            "}"
        )
        self.input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay.addWidget(self.input, 1)

        # 麦克风
        self.voice_btn = _RoundBtn("🎤", "语音输入")
        self.voice_btn.clicked.connect(self.voice_started.emit)
        lay.addWidget(self.voice_btn)

        # 发送（渐变圆）
        self.send_btn = QToolButton()
        self.send_btn.setText("➤")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setToolTip("发送")
        self.send_btn.setFixedSize(38, 38)
        self.send_btn.setStyleSheet(
            "QToolButton {"
            " background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #7b93ff, stop:1 #9d8bff);"
            " border: 1px solid rgba(200,215,255,0.45);"
            " border-radius: 19px; color: #10142e;"
            " font-size: 16px; font-weight: 900;"
            "}"
            "QToolButton:hover {"
            " background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #93a8ff, stop:1 #b3a2ff);"
            "}"
            "QToolButton:pressed { background: #6b83ef; }"
        )
        self.send_btn.clicked.connect(self._on_send)
        lay.addWidget(self.send_btn)

        # Enter 发送
        self.input.installEventFilter(self)

    # ── 事件 ──────────────────────────────────────────────

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent

        if obj is self.input and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _on_send(self):
        text = self.input.toPlainText().strip()
        if text:
            self.message_sent.emit(text)
            self.input.clear()

    # ── 对外接口 ──────────────────────────────────────────

    def insert_text(self, text: str):
        cursor = self.input.textCursor()
        cursor.insertText(text)
        self.input.setTextCursor(cursor)
        self.input.setFocus()

    def get_text(self) -> str:
        return self.input.toPlainText().strip()

    def set_text(self, text: str):
        self.input.setPlainText(text)

    def set_enabled_input(self, enabled: bool):
        self.input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)

    def set_performance_mode(self, mode: str):
        self._perf_mode = mode
        super().set_performance_mode(mode)
