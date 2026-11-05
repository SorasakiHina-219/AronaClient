# core/message_renderer.py
# AronaClient V0.1
# 消息渲染模块

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
import markdown


class MessageRenderer:


    def __init__(self):

        self.extensions = [
            "fenced_code",
            "tables",
            "nl2br",
        ]


    def markdown_to_html(
        self,
        text: str,
    ) -> str:

        if not text:

            return ""

        return markdown.markdown(
            text,
            extensions=self.extensions,
        )


    def render_message(
        self,
        text: str,
    ) -> str:

        return self.markdown_to_html(
            text
        )