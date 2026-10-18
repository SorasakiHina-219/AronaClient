
"""AronaClient 文件下载模块。

受控操作：
    * 从指定 URL 下载文件
    * 逐跳校验重定向，拒绝任何非 HTTPS 跳转
    * 拒绝私有/回环/链路本地 IP 地址
    * DNS 解析失败时 fail-closed
    * 混合公网/私网地址时拒绝
    * 通过 urllib3 HTTPSConnectionPool 绑定到已验证 IP
    * 保留 TLS SNI、证书主机名验证和 HTTP Host 语义
    * 清理文件名，禁止路径穿越
    * 设置下载大小限制和网络超时
    * 默认保存到用户下载目录的独立子目录
    * 下载完成后不自动执行

安全约束：
    * 仅允许 HTTPS，且每个重定向目标也必须为 HTTPS
    * 拒绝私有 IP、回环地址、链路本地地址
    * DNS 解析失败时 fail-closed（拒绝下载）
    * 解析结果为混合公网/私网地址时拒绝
    * 实际 TCP 连接绑定到 DNS 预校验的已验证 IP
    * TLS 证书主机名验证使用原始域名
    * HTTP Host 请求头使用原始域名
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

import urllib3


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


class DnsResolutionError(DownloadError):
    """DNS 解析失败或结果不可信。"""


class Downloader:
    """
    受控文件下载器。

    设计原则：
        * 只下载，不安装、不执行
        * 仅 HTTPS，逐跳校验，拒绝降级
        * 拒绝私有/回环/链路本地 IP
        * DNS 解析失败时 fail-closed
        * 混合公网/私网地址结果时拒绝
        * 实际连接绑定到已验证 IP（urllib3 HTTPSConnectionPool）
        * TLS SNI 和证书验证使用原始域名
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
            content = None
            final_url = current_url

            for redirect_count in range(self._max_redirects + 1):
                # 每次请求前重新解析 DNS 并校验
                hostname = self._get_hostname(current_url)
                if hostname is None:
                    return {
                        "success": False,
                        "url": url,
                        "path": None,
                        "size": None,
                        "sha256": None,
                        "error": "无法从 URL 提取主机名。",
                        "message": "下载失败：URL 格式无效。",
                    }

                dns_result = self._resolve_and_validate_dns(hostname)
                if not dns_result["success"]:
                    return {
                        "success": False,
                        "url": current_url,
                        "path": None,
                        "size": None,
                        "sha256": None,
                        "error": dns_result["error"],
                        "message": dns_result["message"],
                    }

                # 关键安全修复（TOCTOU）：
                # 连接目标必须复用**校验时那一次**解析出的 IP，
                # 不能再调用一次 getaddrinfo —— 否则两次解析之间
                # 存在 DNS 重绑窗口，可能出现「校验的是公网 IP、
                # 实际连的是私网 IP」的绕过。
                resolved_ip = dns_result.get("ip")
                if resolved_ip is None:
                    return {
                        "success": False,
                        "url": current_url,
                        "path": None,
                        "size": None,
                        "sha256": None,
                        "error": "DNS 校验结果缺少可用 IP 地址。",
                        "message": "下载失败：DNS 解析结果不可用。",
                    }

                # 从 URL 提取路径与端口（端口不能硬编码 443，
                # 否则 https://host:8443/ 这类 URL 会连错目标）
                from urllib.parse import urlparse
                parsed = urlparse(current_url)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query

                url_port = parsed.port or 443

                # Host 头在非默认端口时必须带上端口号，否则服务端
                # 会按默认端口理解，语义与原始 URL 不一致。
                host_header = (
                    hostname
                    if url_port == 443
                    else f"{hostname}:{url_port}"
                )

                # 创建连接到已验证 IP 的 pool
                # assert_hostname: TLS 证书验证使用原始域名
                # server_hostname: SNI 使用原始域名
                pool = urllib3.HTTPSConnectionPool(
                    resolved_ip,
                    port=url_port,
                    assert_hostname=hostname,
                    server_hostname=hostname,
                    timeout=self._timeout,
                )

                try:
                    response = pool.request(
                        'GET', path,
                        headers={'Host': host_header},
                        preload_content=True,
                    )

                    # 检查是否是重定向
                    if response.status in (301, 302, 303, 307, 308):
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

                    # 不是重定向，检查状态
                    if response.status >= 400:
                        return {
                            "success": False,
                            "url": url,
                            "path": None,
                            "size": None,
                            "sha256": None,
                            "error": (
                                f"HTTP {response.status}: "
                                f"{response.headers.get('text/plain', '')}"
                            ),
                            "message": f"下载失败：HTTP {response.status}",
                        }

                    final_url = current_url

                    # 校验最终 URL（已在重定向时校验过，此处仅检查是否仍在下载目录内）
                    # 注意：final_url 已在 visited_urls 中（重定向时添加），跳过循环检测
                    from urllib.parse import urlparse as _urlparse
                    _parsed = _urlparse(final_url)
                    if _parsed.scheme.lower() != "https":
                        return {
                            "success": False,
                            "url": url,
                            "path": None,
                            "size": None,
                            "sha256": None,
                            "error": f"最终 URL 不是 HTTPS：{final_url}",
                            "message": "下载失败：最终地址不安全。",
                        }

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

                    content = response.data
                    break

                finally:
                    pool.close()

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

        except urllib3.exceptions.HTTPError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"HTTP 错误：{exc}",
                "message": "下载失败：HTTP 请求错误。",
            }
        except urllib3.exceptions.ConnectTimeoutError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"连接超时：{exc}",
                "message": "下载失败：连接超时。",
            }
        except urllib3.exceptions.ReadTimeoutError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"读取超时：{exc}",
                "message": "下载失败：读取超时。",
            }
        except urllib3.exceptions.NewConnectionError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"连接失败：{exc}",
                "message": "下载失败：无法连接到服务器。",
            }
        except urllib3.exceptions.MaxRetryError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"重试超限：{exc}",
                "message": "下载失败：连接重试超限。",
            }
        except DnsResolutionError as exc:
            return {
                "success": False,
                "url": url,
                "path": None,
                "size": None,
                "sha256": None,
                "error": f"DNS 校验失败：{exc}",
                "message": "下载失败：DNS 解析结果不安全。",
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

    def _get_hostname(self, url):
        """从 URL 中提取主机名。"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.hostname

    def _resolve_and_validate_dns(self, hostname):
        """
        解析 DNS 并校验所有返回的 IP 地址。

        返回：
            {"success": bool, "ip": str|None, "error": str|None,
             "message": str}

        其中 ``ip`` 是**本次校验通过的第一个公网 IP**。
        调用方必须直接用这个 IP 建立连接，不得再次解析 ——
        否则两次解析之间的 DNS 重绑窗口会让连接绕过校验。

        安全策略：
            - DNS 解析失败 → fail-closed（拒绝）
            - 空解析结果 → fail-closed（拒绝）
            - 任何 IP 为私有/回环/链路本地/保留/多播 → 拒绝
            - 混合公网+私网地址 → 拒绝（DNS 重绑嫌疑）
        """
        try:
            addr_info = socket.getaddrinfo(
                hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            # DNS 解析失败 → fail-closed
            return {
                "success": False,
                "error": f"DNS 解析失败：{hostname}",
                "message": "下载失败：无法解析主机名。",
            }

        if not addr_info:
            # 空结果 → fail-closed
            return {
                "success": False,
                "error": f"DNS 解析返回空结果：{hostname}",
                "message": "下载失败：DNS 解析无结果。",
            }

        has_public = False
        has_private = False
        # 记录第一个通过校验的公网 IP，供本次连接直接使用。
        # 这样「校验的 IP」与「连接的 IP」是同一个对象，
        # 不依赖第二次 DNS 解析（消除 TOCTOU 重绑窗口）。
        first_public_ip = None

        for info in addr_info:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                # 无效的 IP 地址 → fail-closed
                return {
                    "success": False,
                    "error": f"DNS 返回无效 IP 地址：{ip_str}",
                    "message": "下载失败：DNS 解析结果异常。",
                }

            is_safe = (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            )

            if is_safe:
                has_private = True
            else:
                has_public = True
                if first_public_ip is None:
                    first_public_ip = ip_str

        # 混合地址 → 拒绝（疑似 DNS 重绑）
        if has_public and has_private:
            return {
                "success": False,
                "error": (
                    f"DNS 解析返回混合地址（公网+私有）：{hostname}"
                ),
                "message": "下载失败：DNS 解析结果包含混合地址。",
            }

        # 全部为私有/不安全地址 → 拒绝
        if has_private and not has_public:
            return {
                "success": False,
                "error": f"DNS 解析所有地址均为私有/保留：{hostname}",
                "message": "下载失败：目标地址为内网地址。",
            }

        # 全部为公网地址 → 放行
        return {"success": True, "ip": first_public_ip}

    def _is_private_or_reserved_ip(self, hostname):
        """
        检查主机名是否解析为私有/回环/链路本地 IP。
        用于重定向校验场景。
        """
        try:
            addr_info = socket.getaddrinfo(
                hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            # DNS 解析失败 → fail-closed
            return True

        if not addr_info:
            # 空结果 → fail-closed
            return True

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
                # 无效 IP → fail-closed
                return True

        return False

    def _resolve_redirect_url(self, base_url, location):
        """
        解析重定向 URL，处理相对路径。

        必须用**完整 base_url**（含路径）做 RFC 3986 解析：
        例如 base=https://a.com/x/y、location='f.txt'
        正确结果是 https://a.com/x/f.txt，
        而不是丢掉 /x/ 变成 https://a.com/f.txt
        （后者会让重定向落到错误的资源上）。
        """
        from urllib.parse import urljoin

        return urljoin(base_url, location)

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
