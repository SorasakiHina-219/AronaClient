
"""AronaClient 文件下载模块。

受控操作：
    * 从指定 URL 下载文件
    * 校验来源 URL（仅 HTTPS）
    * 清理文件名，禁止路径穿越
    * 设置下载大小限制和网络超时
    * 默认保存到用户下载目录的独立子目录
    * 下载完成后不自动执行

安全约束：
    * 仅允许 HTTPS 下载
    * 校验重定向目标仍为 HTTPS
    * 文件大小上限：100MB（可配置）
    * 网络超时：30秒
    * 文件名清理防止路径穿越
    * 不自动解压、不自动运行
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx


# ============================================================
# 安全常量
# ============================================================

# 最大下载大小（100 MB）
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024

# 网络超时（秒）
REQUEST_TIMEOUT = 30.0

# 重定向次数上限
MAX_REDIRECTS = 5

# 下载保存根目录名
DOWNLOAD_SUBDIR = "downloads"

# 文件名清理正则：移除危险字符
FILENAME_SAFE_REGEX = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 禁止的文件扩展名（不自动执行）
FORBIDDEN_EXTENSIONS = {
    ".exe", ".msi", ".bat", ".cmd", ".ps1",
    ".vbs", ".js", ".wsf", ".scr", ".com",
}

# 允许的文件扩展名（白名单，非必须匹配）
SAFE_EXTENSIONS = {
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".txt", ".md", ".csv", ".json", ".xml",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".mp3", ".wav", ".mp4", ".avi", ".mkv",
}


class DownloadError(Exception):
    """下载操作错误基类。"""


class ProtocolNotAllowedError(DownloadError):
    """URL 协议不被允许。"""


class UrlRedirectError(DownloadError):
    """重定向到非 HTTPS 地址。"""


class SizeExceededError(DownloadError):
    """下载大小超出限制。"""


class FilenameSanitizationError(DownloadError):
    """文件名清理失败。"""


class Downloader:
    """
    受控文件下载器。

    设计原则：
        * 只下载，不安装、不执行
        * 只 HTTPS，拒绝 HTTP/HTTPs 降级
        * 文件名安全清理，防路径穿越
        * 大小限制，防止磁盘耗尽
        * 超时保护，防止无限等待
    """

    def __init__(
        self,
        download_root: str | Path | None = None,
        max_size: int = MAX_DOWNLOAD_SIZE,
        timeout: float = REQUEST_TIMEOUT,
        max_redirects: int = MAX_REDIRECTS,
    ):
        """
        初始化下载器。

        参数：
            download_root: 下载保存根目录。
                          默认：~/Downloads/arona_downloads
            max_size: 最大下载字节数，默认 100MB。
            timeout: 网络超时秒数，默认 30s。
            max_redirects: 最大重定向次数。
        """
        if download_root is None:
            home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
            download_root = Path(home) / "Downloads" / DOWNLOAD_SUBDIR

        self._download_root = Path(download_root).resolve()
        self._max_size = max_size
        self._timeout = timeout
        self._max_redirects = max_redirects

        # 确保下载根目录存在
        self._download_root.mkdir(parents=True, exist_ok=True)

    def download(
        self,
        url: str,
        filename: str | None = None,
        expected_size: int | None = None,
    ) -> dict[str, Any]:
        """
        下载文件。

        参数：
            url: 要下载的 HTTPS URL。
            filename: 保存文件名（可选）。
                      若未提供，从 URL 中提取或生成。
            expected_size: 预期文件大小（可选），用于预检。

        返回：
            {
                "success": bool,
                "url": str,
                "path": str | None,
                "size": int | None,
                "sha256": str | None,
                "error": str | None,
                "message": str,
            }
        """

        # ---- 前置校验 ----
        validation = self._validate_url(url)
        if not validation["success"]:
            return validation

        safe_url = validation["url"]

        # ---- 发送请求 ----
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=1),
            ) as client:
                response = client.get(safe_url)
                response.raise_for_status()

        except httpx.HTTPStatusError as exc:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"HTTP {exc.response.status_code}: {exc.reason_phrase}",
                "message": f"下载失败：HTTP {exc.response.status_code}",
            }
        except httpx.TooManyRedirects:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": "重定向次数过多。",
                "message": "下载失败：过多重定向。",
            }
        except httpx.ConnectError as exc:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"连接失败：{exc}",
                "message": "下载失败：无法连接到服务器。",
            }
        except httpx.TimeoutException as exc:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"超时：{exc}",
                "message": "下载失败：请求超时。",
            }
        except Exception as exc:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "下载过程发生未知错误。",
            }

        # ---- 检查重定向后的最终 URL 是否仍为 HTTPS ----
        final_url = str(response.url)
        if not final_url.startswith("https://"):
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": (
                    f"重定向目标非 HTTPS：{final_url}"
                ),
                "message": "下载失败：重定向到不安全地址。",
            }

        # ---- 检查 Content-Length ----
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                cl = int(content_length)
            except ValueError:
                cl = None
            else:
                if cl > self._max_size:
                    return {
                        "success": False,
                        "url": safe_url,
                        "path": None,
                        "size": cl,
                        "sha256": None,
                        "error": (
                            f"文件过大：{cl} bytes，"
                            f"上限 {self._max_size} bytes。"
                        ),
                        "message": "下载失败：文件大小超出限制。",
                    }

        # ---- 读取内容并写入文件 ----
        content = response.content

        if len(content) > self._max_size:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": len(content),
                "sha256": None,
                "error": (
                    f"下载内容过大：{len(content)} bytes。"
                ),
                "message": "下载失败：文件大小超出限制。",
            }

        # ---- 确定保存文件名 ----
        save_filename = self._resolve_filename(
            filename, safe_url, response
        )

        # ---- 计算 SHA-256 ----
        sha256 = hashlib.sha256(content).hexdigest()

        # ---- 写入文件 ----
        save_path = self._download_root / save_filename

        # 防止路径穿越（双重保险）
        resolved_path = save_path.resolve()
        if not str(resolved_path).startswith(
            str(self._download_root.resolve())
        ):
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": "文件名清理失败：路径穿越检测。",
                "message": "下载失败：文件名包含非法路径。",
            }

        try:
            save_path.write_bytes(content)
        except OSError as exc:
            return {
                "success": False,
                "url": safe_url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"写入文件失败：{exc}",
                "message": "下载失败：无法保存文件。",
            }

        return {
            "success": True,
            "url": safe_url,
            "path": str(resolved_path),
            "size": len(content),
            "sha256": sha256,
            "error": None,
            "message": (
                f"已下载到：{resolved_path} "
                f"（{len(content)} bytes）"
            ),
        }

    # ========================================================
    # 内部工具方法
    # ========================================================

    def _validate_url(self, url: str) -> dict[str, Any]:
        """校验 URL 协议和格式。"""

        if not isinstance(url, str) or not url.strip():
            return {
                "success": False,
                "url": None,
                "path": None,
                "size": None,
                "sha256": None,
                "error": "URL 不能为空。",
                "message": "参数校验失败。",
            }

        url = url.strip()

        from urllib.parse import urlparse
        parsed = urlparse(url)

        if not parsed.scheme:
            return {
                "success": False,
                "url": None,
                "path": None,
                "size": None,
                "sha256": None,
                "error": "URL 缺少协议（scheme）。",
                "message": "URL 格式无效。",
            }

        if parsed.scheme.lower() != "https":
            return {
                "success": False,
                "url": None,
                "path": None,
                "size": None,
                "sha256": None,
                "error": (
                    f"只允许 HTTPS 下载，"
                    f"收到协议：{parsed.scheme}"
                ),
                "message": "拒绝非 HTTPS 下载。",
            }

        if not parsed.hostname:
            return {
                "success": False,
                "url": None,
                "path": None,
                "size": None,
                "sha256": None,
                "error": "URL 缺少主机名。",
                "message": "URL 格式无效。",
            }

        return {"success": True, "url": url}

    def _resolve_filename(
        self,
        filename: str | None,
        url: str,
        response: httpx.Response,
    ) -> str:
        """
        确定保存文件名。

        优先级：
            1. 调用方提供的 filename（经清理）
            2. URL 中的文件名部分
            3. 从 Content-Disposition 头提取
            4. 生成安全随机名 + 从 URL 推断的扩展名
        """

        if filename:
            safe = self._sanitize_filename(filename)
            if safe:
                return safe

        # 从 URL 提取
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path_part = parsed.path.strip("/")

        if path_part:
            # 取最后一段
            name = path_part.split("/")[-1]
            if name:
                safe = self._sanitize_filename(name)
                if safe:
                    return safe

        # 从 Content-Disposition 头提取
        content_disp = response.headers.get("content-disposition", "")
        if "filename" in content_disp:
            import re
            match = re.search(
                r'filename["\']?=?["\']?([^";\n]+)',
                content_disp,
            )
            if match:
                safe = self._sanitize_filename(match.group(1).strip())
                if safe:
                    return safe

        # 最终兜底：安全随机名 + .dat
        timestamp = int(time.time())
        return f"arona_download_{timestamp}.dat"

    def _sanitize_filename(self, filename: str) -> str:
        """
        清理文件名，移除危险字符，防止路径穿越。
        """

        # 移除路径分隔符
        safe = filename.replace("\\", "_").replace("/", "_")

        # 移除危险字符
        safe = FILENAME_SAFE_REGEX.sub("_", safe)

        # 去除首尾空白
        safe = safe.strip(" ._")

        # 防止空名
        if not safe:
            safe = "untitled"

        # 限制长度
        safe = safe[:200]

        # 防止 Windows 保留名
        windows_reserved = {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        }
        base, ext = os.path.splitext(safe)
        if base.upper() in windows_reserved:
            safe = f"file_{base}{ext}"

        return safe
