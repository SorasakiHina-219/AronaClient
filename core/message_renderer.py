# core/message_renderer.py
# AronaClient V0.1
# 消息渲染模块

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