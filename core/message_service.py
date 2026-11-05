# core/message_service.py
# AronaClient V0.1
# 消息服务层
#
# UI 不直接调用 AIClient
# 所有消息流程统一经过这里


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
from core.chat_manager import ChatManager
from core.chat_message import ChatMessage


class MessageService:


    def __init__(
        self,
        ai_client,
    ):

        self.ai_client = ai_client

        self.chat_manager = ChatManager()


    # =====================================================
    # 发送消息
    # =====================================================

    def send_message(
        self,
        text,
        attachments=None,
    ):

        attachments = attachments or []


        # 保存用户消息

        user_message = ChatMessage(
            role="user",
            content=text,
            attachments=attachments,
        )

        self.chat_manager.add_message(
            user_message
        )


        # 调用 AI

        result = self.ai_client.send_message(
            text,
            attachments,
        )


        # 保存 AI 回复

        assistant_message = ChatMessage(
            role="assistant",
            content=result,
        )

        self.chat_manager.add_message(
            assistant_message
        )


        return result



    # =====================================================
    # 获取历史
    # =====================================================

    def get_history(self):

        return (
            self.chat_manager
            .get_messages()
        )



    # =====================================================
    # 清空聊天
    # =====================================================

    def clear(self):

        self.chat_manager.clear()