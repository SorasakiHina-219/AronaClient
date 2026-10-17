# core/message_renderer.py
# AronaClient V0.1
# 消息渲染模块

import markdown


class MessageRenderer:
    """
    Arona 消息渲染器

    负责：
    - Markdown 转 HTML
    - AI 回复格式处理
    """


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

        """
        UI 调用入口
        """

        return self.markdown_to_html(
            text
        )