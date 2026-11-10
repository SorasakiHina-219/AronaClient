"""
spatial_ui.bubble — 聊天气泡组件。

- AI 消息：左对齐，深蓝玻璃气泡 + 淡紫描边
- 用户消息：右对齐，蓝紫渐变气泡
- 系统消息：居中提示条
全部使用深色玻璃语义，不出现原生白底控件。
"""

import warnings
warnings.filterwarnings("ignore")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QTextEdit,
)

from spatial_ui.styles import (
    BUBBLE_RADIUS,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_MUTED,
    COLOR_ACCENT_PINK,
)


class MessageBubble(QWidget):
    """单条聊天消息气泡。role: assistant / user / system"""

    def __init__(
        self,
        role: str,
        content: str = "",
        avatar_initial: str = "",
        parent=None,
        *,
        bubble_opacity: float = 0.98,
        bubble_radius: int = BUBBLE_RADIUS,
    ):
        super().__init__(parent)

        self._role = role
        self._radius = bubble_radius
        self._alpha = int(max(0.2, min(1.0, bubble_opacity)) * 255)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        if role == "system":
            self._build_system(outer)
            return

        is_user = role == "user"

        # 头像
        self.avatar_label = QLabel(
            avatar_initial or ("U" if is_user else "A")
        )
        self.avatar_label.setAlignment(Qt.AlignCenter)
        self.avatar_label.setFixedSize(34, 34)
        self.avatar_label.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 {c1}, stop:1 {c2});"
            " border: 1px solid rgba(220,230,255,0.40);"
            " border-radius: 17px; color: #10142e;"
            " font-weight: 800; font-size: 13px;".format(
                c1="#93a8ff" if is_user else "#b3a2ff",
                c2="#c0a6ff" if is_user else "#8ea6ff",
            )
        )

        # 内容
        col = QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)

        name = QLabel("我" if is_user else "Arona")
        name.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 10px;"
            f" background: transparent;"
            + (" qproperty-alignment: AlignRight;" if is_user else "")
        )

        self.text_widget = QTextEdit()
        self.text_widget.setReadOnly(True)
        self.text_widget.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
        )
        self.text_widget.setMaximumWidth(640)
        self.text_widget.setMinimumHeight(34)
        self.text_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_widget.setObjectName("bubbleText")
        self.text_widget.setStyleSheet(
            "QTextEdit#bubbleText {"
            " background: transparent; border: none;"
            " padding: 8px 12px;"
            " color: #eef1ff; font-size: 13px;"
            "}"
        )

        col.addWidget(name, alignment=Qt.AlignRight if is_user else Qt.AlignLeft)
        col.addWidget(self.text_widget, alignment=Qt.AlignRight if is_user else Qt.AlignLeft)

        if is_user:
            outer.addStretch(1)
            outer.addLayout(col)
            outer.addWidget(self.avatar_label, 0, Qt.AlignTop)
        else:
            outer.addWidget(self.avatar_label, 0, Qt.AlignTop)
            outer.addLayout(col)
            outer.addStretch(1)

        self._apply_bubble_style(is_user)
        self.setText(content)

    # ── 样式 ──────────────────────────────────────────────

    def _apply_bubble_style(self, is_user: bool):
        if is_user:
            bg = f"rgba(123, 147, 255, {self._alpha})"
            border = "rgba(205, 218, 255, 0.55)"
            text = "#0f1330"
        else:
            bg = f"rgba(55, 63, 114, {self._alpha})"
            border = "rgba(168, 182, 255, 0.42)"
            text = "#eef1ff"

        self.text_widget.setStyleSheet(
            "QTextEdit#bubbleText {"
            f" background: {bg};"
            f" border: 1px solid {border};"
            f" border-radius: {self._radius}px;"
            f" color: {text};"
            " font-size: 13px;"
            " padding: 9px 13px;"
            "}"
        )

    def _build_system(self, outer: QHBoxLayout):
        outer.addStretch(1)
        label = QLabel("")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: rgba(255,255,255,0.05);"
            f" border: 1px solid rgba(150,172,255,0.16);"
            f" border-radius: 10px; padding: 5px 12px;"
        )
        self._system_label = label
        outer.addWidget(label)
        outer.addStretch(1)

    # ── 内容 ──────────────────────────────────────────────

    def setText(self, text: str):
        if self._role == "system":
            self._system_label.setText(text)
            return

        html = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        color = "#0f1330" if self._role == "user" else "#eef1ff"
        self.text_widget.setHtml(
            f'<div style="color:{color}; font-size:13px;'
            f' line-height:1.55;">{html}</div>'
        )
        self.text_widget.document().setTextWidth(560)
        h = int(self.text_widget.document().size().height()) + 22
        self.text_widget.setFixedHeight(max(36, min(h, 420)))

    def setImage(self, image_path: str):
        """在气泡内嵌入图片（宽度自适应）。"""
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            return
        target = pixmap.scaled(
            380, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        safe = str(image_path).replace("\\", "/")
        self.text_widget.setHtml(
            f'<img src="{safe}" width="{target.width()}" '
            f'height="{target.height()}">'
        )
        self.text_widget.setFixedHeight(min(320, target.height() + 24))

    def get_role(self) -> str:
        return self._role

    def get_text(self) -> str:
        return (
            self._system_label.text()
            if self._role == "system"
            else self.text_widget.toPlainText()
        )
