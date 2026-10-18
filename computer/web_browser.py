
"""AronaClient 浏览器与网址控制模块。

受控操作：
    * 打开默认浏览器访问指定网址
    * 校验网址协议，只允许 http/https
    * 拒绝 javascript:/file:/data:/ 等危险协议
    * 返回执行结果（成功/失败原因）

安全约束：
    * 不自动导航到模型生成的任意 URL
    * 用户必须明确指定网址才能打开
    * 不执行网页脚本、不填写表单、不提交数据
"""

from __future__ import annotations

import webbrowser
from urllib.parse import urlparse
from typing import Any


# ============================================================
# 允许协议的白名单
# ============================================================
ALLOWED_SCHEMES = {"http", "https"}

# 禁止协议的黑名单（白名单之外的全部拒绝）
DANGEROUS_SCHEMES = {"javascript", "file", "data", "ftp", "blob", "about"}

# 最大 URL 长度，防止超长参数注入
MAX_URL_LENGTH = 8192


class WebBrowserError(Exception):
    """浏览器操作的错误基类。"""


class ProtocolNotAllowedError(WebBrowserError):
    """URL 协议不在白名单中。"""


class UrlTooLongError(WebBrowserError):
    """URL 超出允许的最大长度。"""


class BrowserController:
    """
    受控浏览器导航控制器。

    设计原则：
        * 不解析模型输出 —— 调用方传入明确的 URL
        * 不做 SPAM-like 批量打开
        * 不开标签页（webbrowser 默认行为因平台而异，不保证）
        * 不开新浏览器实例（复用默认浏览器）
    """

    def open_url(self, url: str) -> dict[str, Any]:
        """
        在默认浏览器中打开指定 URL。

        参数：
            url: 要打开的完整 URL（必须包含协议）。

        返回：
            {
                "success": bool,
                "url": str,
                "error": str | None,
                "message": str,
            }
        """

        # ---- 前置校验 ----
        result = self._validate_url(url)
        if not result["success"]:
            return result

        validated_url = result["url"]

        # ---- 执行导航 ----
        try:
            opened = webbrowser.open(validated_url, new=1, autoraise=True)
        except Exception as exc:
            return {
                "success": False,
                "url": validated_url,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "浏览器打开失败。",
            }

        if opened:
            return {
                "success": True,
                "url": validated_url,
                "error": None,
                "message": f"已在默认浏览器中打开：{validated_url}",
            }
        else:
            return {
                "success": False,
                "url": validated_url,
                "error": None,
                "message": (
                    "默认浏览器未能启动，请检查系统浏览器配置。"
                ),
            }

    # ========================================================
    # 内部工具方法
    # ========================================================

    def _validate_url(self, url: str) -> dict[str, Any]:
        """
        校验 URL 格式与协议。

        返回经过清理的 URL 或错误信息。
        """

        if not isinstance(url, str):
            return {
                "success": False,
                "url": None,
                "error": "URL 必须是字符串。",
                "message": "参数类型错误。",
            }

        url = url.strip()

        if not url:
            return {
                "success": False,
                "url": None,
                "error": "URL 不能为空。",
                "message": "参数校验失败。",
            }

        if len(url) > MAX_URL_LENGTH:
            return {
                "success": False,
                "url": None,
                "error": f"URL 长度超过上限（{MAX_URL_LENGTH}）。",
                "message": "URL 太长，拒绝打开。",
            }

        parsed = urlparse(url)

        # 缺少 scheme（如 "www.example.com"）→ 补全为 https
        if not parsed.scheme:
            url = f"https://{url}"
            parsed = urlparse(url)

        scheme = parsed.scheme.lower()

        if scheme in DANGEROUS_SCHEMES:
            return {
                "success": False,
                "url": None,
                "error": (
                    f"协议 '{scheme}' 被禁止。"
                    "只允许 http 和 https。"
                ),
                "message": f"拒绝访问 {scheme}:// 协议。",
            }

        if scheme not in ALLOWED_SCHEMES:
            return {
                "success": False,
                "url": None,
                "error": (
                    f"未知协议 '{scheme}'。"
                    "只允许 http 和 https。"
                ),
                "message": f"未知协议被拒绝：{scheme}",
            }

        # 额外检查：hostname 不能是空字符串（排除 "http:///" 这类注入）
        if not parsed.hostname:
            return {
                "success": False,
                "url": None,
                "error": "URL 缺少有效主机名。",
                "message": "URL 格式无效。",
            }

        return {
            "success": True,
            "url": url,
            "error": None,
            "message": None,
        }
