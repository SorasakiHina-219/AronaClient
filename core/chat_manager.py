# core/chat_manager.py
# AronaClient V0.1
# 聊天消息管理模块


from PySide6.QtWidgets import QApplication

from core.chat_message import ChatMessage



class ChatManager:


    def __init__(
        self,
        layout,
        scroll_area=None,
    ):

        self.layout = layout

        self.scroll_area = scroll_area



    def add_message(
        self,
        role,
        text,
    ):

        message = ChatMessage(
            role,
            text,
        )


        self.layout.insertWidget(
            self.layout.count() - 1,
            message,
        )


        QApplication.processEvents()


        self.scroll_to_bottom()



    def clear(self):

        while self.layout.count() > 1:

            item = self.layout.takeAt(0)

            widget = item.widget()

            if widget:

                widget.deleteLater()



    def scroll_to_bottom(
        self,
    ):

        if self.scroll_area is None:

            return


        bar = (
            self.scroll_area
            .verticalScrollBar()
        )


        bar.setValue(
            bar.maximum()
        )