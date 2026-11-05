# core/chat_manager.py
# AronaClient V0.1
# 聊天消息管理模块


# -*- coding: utf-8 -*-
# Copyright (C) 2024-2026 SorasakiHina-219
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
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