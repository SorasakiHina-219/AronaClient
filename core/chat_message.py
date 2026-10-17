# core/chat_message.py
# AronaClient V0.1
# 聊天消息组件

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QHBoxLayout,
    QVBoxLayout,
    QTextBrowser,
)

from core.message_renderer import MessageRenderer


class ChatMessage(QFrame):

    def __init__(
        self,
        role,
        text,
        parent=None,
    ):

        super().__init__(parent)

        self.renderer = MessageRenderer()

        self.role = role

        self.build(text)


    def build(
        self,
        text,
    ):

        row = QHBoxLayout(self)

        row.setSpacing(10)

        bubble = QFrame()

        if self.role == "assistant":
            bubble.setObjectName(
                "aiBubble"
            )

        else:
            bubble.setObjectName(
                "userBubble"
            )


        bubble_layout = QVBoxLayout(
            bubble
        )

        bubble_layout.setContentsMargins(
            14,
            10,
            14,
            10,
        )


        name = QLabel(
            "Arona"
            if self.role == "assistant"
            else "你"
        )

        name.setObjectName(
            "messageName"
        )


        content = QTextBrowser()

        content.setObjectName(
            "messageText"
        )

        content.setFrameShape(
            QFrame.NoFrame
        )

        content.setOpenExternalLinks(
            True
        )

        content.setStyleSheet(
            """
            QTextBrowser {
                background: transparent;
                border: none;
            }
            """
        )


        html = self.renderer.render_message(
            text
        )

        content.setHtml(
            html
        )

        content.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )


        bubble_layout.addWidget(
            name
        )

        bubble_layout.addWidget(
            content
        )


        if self.role == "assistant":

            avatar = QLabel(
                "A"
            )

            avatar.setObjectName(
                "messageAvatar"
            )

            avatar.setAlignment(
                Qt.AlignCenter
            )


            row.addWidget(
                avatar,
                alignment=Qt.AlignTop,
            )

            row.addWidget(
                bubble
            )

            row.addStretch()


        else:

            row.addStretch()

            row.addWidget(
                bubble
            )

            avatar = QLabel(
                "●"
            )

            avatar.setObjectName(
                "userAvatar"
            )

            avatar.setAlignment(
                Qt.AlignCenter
            )


            row.addWidget(
                avatar,
                alignment=Qt.AlignTop,
            )