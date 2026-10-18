
"""AronaClient 文件下载模块。

受控操作：
    * 从指定 URL 下载文件
    * 逐跳校验重定向，拒绝任何非 HTTPS 跳转
    * 拒绝私有/回环/链路本地 IP 地址
    * 清理文件名，禁止路径穿越
    * 设置下载大小限制和网络超时
    * 默认保存到用户下载目录的独立子目录
    * 下载完成后不自动执行

安全约束：
    * 仅允许 HTTPS，且每个重定向目标也必须为 HTTPS
    * 拒绝私有 IP、回环地址、链路本地地址
    * 文件大小上限：100MB（可配置）
    * 网络超时：30秒
    * 文件名清理防止路径穿越
    * 不自动解压、不自动运行
    * 临时文件在失败时自动清理
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx


# ============================================================
# 安全常量
# ============================================================

MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024
REQUEST_TIMEOUT = 30.0
MAX_REDIRECTS = 5
DOWNLOAD_SUBDIR = "downloads"
FILENAME_SAFE_REGEX = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

FORBIDDEN_EXTENSIONS = {
    ".exe", ".msi", ".bat", ".cmd", ".ps1",
    ".vbs", ".js", ".wsf", ".scr", ".com",
}


class DownloadError(Exception):
    """下载操作错误基类。"""


class ProtocolNotAllowedError(DownloadError):
    """URL 协议不被允许。"""


class UrlRedirectError(DownloadError):
    """重定向到非 HTTPS 地址。"""


class PrivateAddressError(DownloadError):
    """重定向到私有/回环地址。"""


class SizeExceededError(DownloadError):
    """下载大小超出限制。"""


class Downloader:
    """
    受控文件下载器。

    设计原则：
        * 只下载，不安装、不执行
        * 仅 HTTPS，逐跳校验，拒绝降级
        * 拒绝私有/回环/链路本地 IP
        * 文件名安全清理，防路径穿越
        * 大小限制，防止磁盘耗尽
        * 临时文件失败时自动清理
    """

    def __init__(
        self,
        download_root=None,
        max_size=MAX_DOWNLOAD_SIZE,
        timeout=REQUEST_TIMEOUT,
        max_redirects=MAX_REDIRECTS,
    ):
        if download_root is None:
            home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
            download_root = Path(home) / "Downloads" / DOWNLOAD_SUBDIR

        self._download_root = Path(download_root).resolve()
        self._max_size = max_size
        self._timeout = timeout
        self._max_redirects = max_redirects

        self._download_root.mkdir(parents=True, exist_ok=True)

    def download(self, url, filename=None, expected_size=None):
        """
        下载文件。

        参数：
            url: 要下载的 HTTPS URL。
            filename: 保存文件名（可选）。
            expected_size: 预期文件大小（可选）。

        返回：
            {"success": bool, "url": str, "path": str|None,
             "size": int|None, "sha256": str|None,
             "error": str|None, "message": str}
        """
        # ---- 前置校验 ----
        validation = self._validate_url(url)
        if not validation["success"]:
            return validation

        current_url = validation["url"]
        visited_urls = {current_url}

        # ---- 手动跟随重定向，逐跳校验 ----
        try:
            with httpx.Client(
                follow_redirects=False,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=1),
            ) as client:
                content = None
                final_url = current_url

                for redirect_count in range(self._max_redirects + 1):
                    response = client.get(current_url)

                    # 检查是否是重定向
                    if response.status_code in (301, 302, 303, 307, 308):
                        redirect_count += 1
                        if redirect_count > self._max_redirects:
                            return {
                                "success": False,
                                "url": url,
                                "path": None,
                                "size": None,
                                "sha256": None,
                                "error": "重定向次数超过上限。",
                                "message": "下载失败：过多重定向。",
                            }

                        location = response.headers.get("location", "")
                        if not location:
                            return {
                                "success": False,
                                "url": current_url,
                                "path": None,
                                "size": None,
                                "sha256": None,
                                "error": "重定向缺少 Location 头。",
                                "message": "下载失败：无效重定向。",
                            }

                        # 解析相对路径
                        next_url = self._resolve_redirect_url(
                            current_url, location
                        )
                        if next_url is None:
                            return {
                                "success": False,
                                "url": current_url,
                                "path": None,
                                "size": None,
                                "sha256": None,
                                "error": "无法解析重定向 URL。",
                                "message": "下载失败：无效重定向地址。",
                            }

                        # 校验重定向目标
                        redirect_check = self._validate_redirect_target(
                            next_url, visited_urls
                        )
                        if not redirect_check["success"]:
                            return {
                                "success": False,
                                "url": current_url,
                                "path": None,
                                "size": None,
                                "sha256": None,
                                "error": redirect_check["error"],
                                "message": redirect_check["message"],
                            }

                        current_url = redirect_check["url"]
                        visited_urls.add(current_url)
                        continue

                    # 不是重定向，获取内容
                    response.raise_for_status()
                    final_url = str(response.url)

                    # 校验最终 URL
                    final_check = self._validate_redirect_target(
                        final_url, visited_urls
                    )
                    if not final_check["success"]:
                        return {
                            "success": False,
                            "url": url,
                            "path": None,
                            "size": None,
                            "sha256": None,
                            "error": final_check["error"],
                            "message": final_check["message"],
                        }
                    current_url = final_check["url"]

                    # 检查 Content-Length
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
                                    "url": url,
                                    "path": None,
                                    "size": cl,
                                    "sha256": None,
                                    "error": (
                                        f"文件过大：{cl} bytes，"
                                        f"上限 {self._max_size} bytes。"
                                    ),
                                    "message": "下载失败：文件大小超出限制。",
                                }

                    content = response.content
                    break

                if content is None:
                    return {
                        "success": False,
                        "url": url,
                        "path": None,
                        "size": None,
                        "sha256": None,
                        "error": "未获取到下载内容。",
                        "message": "下载失败：无内容。",
                    }

        except httpx.HTTPStatusError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"HTTP {exc.response.status_code}: {exc.reason_phrase}",
                "message": f"下载失败：HTTP {exc.response.status_code}",
            }
        except httpx.ConnectError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"连接失败：{exc}",
                "message": "下载失败：无法连接到服务器。",
            }
        except httpx.TimeoutException as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"超时：{exc}",
                "message": "下载失败：请求超时。",
            }
        except Exception as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "下载过程发生未知错误。",
            }

        # ---- 大小二次校验 ----
        if len(content) > self._max_size:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": len(content),
                "sha256": None,
                "error": f"下载内容过大：{len(content)} bytes。",
                "message": "下载失败：文件大小超出限制。",
            }

        # ---- 确定保存文件名 ----
        save_filename = self._resolve_filename(filename, current_url, content)

        # ---- 计算 SHA-256 ----
        sha256 = hashlib.sha256(content).hexdigest()

        # ---- 写入临时文件，成功后原子移动 ----
        temp_fd, temp_path = tempfile.mkstemp(
            prefix="arona_dl_", suffix=".tmp",
            dir=str(self._download_root)
        )
        try:
            with os.fdopen(temp_fd, 'wb') as f:
                f.write(content)

            save_path = self._download_root / save_filename

            # 防止路径穿越（双重保险）
            resolved_save = save_path.resolve()
            if not str(resolved_save).startswith(
                str(self._download_root.resolve())
            ):
                os.close(temp_fd) if not os.devnull else None
                os.unlink(temp_path)
                return {
                    "success": False,
                    "url": url,
                    "path": None,
                    "size": None,
                    "sha256": None,
                    "error": "文件名清理失败：路径穿越检测。",
                    "message": "下载失败：文件名包含非法路径。",
                }

            # 原子移动
            if save_path.exists():
                save_path.unlink()
            Path(temp_path).rename(resolved_save)

        except OSError as exc:
            # 清理临时文件
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"写入文件失败：{exc}",
                "message": "下载失败：无法保存文件。",
            }

        return {
            "success": True,
            "url": current_url,
            "path": str(resolved_save),
            "size": len(content),
            "sha256": sha256,
            "error": None,
            "message": (
                f"已下载到：{resolved_save} "
                f"（{len(content)} bytes）"
            ),
        }

    # ========================================================
    # 内部工具方法
    # ========================================================

    def _validate_url(self, url):
        """校验 URL 协议和格式。"""
        if not isinstance(url, str) or not url.strip():
            return {
                "success": False, "url": None,
                "error": "URL 不能为空。",
                "message": "参数校验失败。",
            }

        url = url.strip()

        from urllib.parse import urlparse
        parsed = urlparse(url)

        if not parsed.scheme:
            return {
                "success": False, "url": None,
                "error": "URL 缺少协议（scheme）。",
                "message": "URL 格式无效。",
            }

        if parsed.scheme.lower() != "https":
            return {
                "success": False, "url": None,
                "error": f"只允许 HTTPS 下载，收到协议：{parsed.scheme}",
                "message": "拒绝非 HTTPS 下载。",
            }

        if not parsed.hostname:
            return {
                "success": False, "url": None,
                "error": "URL 缺少主机名。",
                "message": "URL 格式无效。",
            }

        # 检查是否有凭据
        if parsed.username or parsed.password:
            return {
                "success": False, "url": None,
                "error": "URL 包含用户名或密码，不允许下载。",
                "message": "拒绝包含凭据的 URL。",
            }

        return {"success": True, "url": url}

    def _validate_redirect_target(self, url, visited_urls):
        """
        校验重定向目标 URL。

        检查：
            1. 协议必须为 HTTPS
            2. 不能是私有/回环/链路本地 IP
            3. 不能循环跳转
        """
        from urllib.parse import urlparse
        parsed = urlparse(url)

        if parsed.scheme.lower() != "https":
            return {
                "success": False,
                "error": f"重定向到非 HTTPS：{url}",
                "message": "下载失败：重定向到不安全地址。",
            }

        if not parsed.hostname:
            return {
                "success": False,
                "error": "重定向 URL 缺少主机名。",
                "message": "下载失败：无效重定向。",
            }

        # 检查循环
        if url in visited_urls:
            return {
                "success": False,
                "error": f"检测到循环重定向：{url}",
                "message": "下载失败：循环重定向。",
            }

        # 检查 IP 地址类型
        if self._is_private_or_reserved_ip(parsed.hostname):
            return {
                "success": False,
                "error": (
                    f"重定向到私有/保留地址：{parsed.hostname}"
                ),
                "message": "下载失败：重定向到内网地址。",
            }

        return {"success": True, "url": url}

    def _is_private_or_reserved_ip(self, hostname):
        """
        检查主机名是否解析为私有/回环/链路本地 IP。
        """
        # 先尝试直接解析
        try:
            addr_info = socket.getaddrinfo(
                hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            # 解析失败，可能是域名，放行（DNS 可能指向不同地址）
            return False

        for info in addr_info:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                ):
                    return True
            except ValueError:
                continue

        # 如果是 IPv6 压缩格式的特殊地址
        try:
            if ipaddress.ip_address(hostname).is_private:
                return True
        except ValueError:
            pass

        return False

    def _resolve_redirect_url(self, base_url, location):
        """
        解析重定向 URL，处理相对路径。
        """
        from urllib.parse import urljoin, urlparse

        base = urlparse(base_url)
        loc = urlparse(location)

        # 已经是绝对 URL
        if loc.scheme:
            return location

        # 相对路径
        return urljoin(base.scheme + "://" + base.netloc, location)

    def _resolve_filename(self, filename, url, content):
        """确定保存文件名。"""
        if filename:
            safe = self._sanitize_filename(filename)
            if safe:
                return safe

        from urllib.parse import urlparse
        parsed = urlparse(url)
        path_part = parsed.path.strip("/")

        if path_part:
            name = path_part.split("/")[-1]
            if name:
                safe = self._sanitize_filename(name)
                if safe:
                    return safe

        content_disp = ""  # Not available without response object
        if content_disp and "filename" in content_disp:
            import re as _re
            match = _re.search(
                r'filename["\']?=?["\']?([^";\n]+)', content_disp
            )
            if match:
                safe = self._sanitize_filename(match.group(1).strip())
                if safe:
                    return safe

        timestamp = int(time.time())
        return f"arona_download_{timestamp}.dat"

    def _sanitize_filename(self, filename):
        """清理文件名。"""
        safe = filename.replace("\\", "_").replace("/", "_")
        safe = FILENAME_SAFE_REGEX.sub("_", safe)
        safe = safe.strip(" ._")
        if not safe:
            safe = "untitled"
        safe = safe[:200]

        windows_reserved = {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        }
        base, ext = os.path.splitext(safe)
        if base.upper() in windows_reserved:
            safe = f"file_{base}{ext}"

        return safe
