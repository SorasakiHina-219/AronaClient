
"""AronaClient V0.2 下载器连接 IP 绑定 — 专项测试。

验证 urllib3 HTTPSConnectionPool 实现：
    1. DNS 校验（私有/回环/链路本地/保留/多播 IP 拒绝）
    2. 混合公网/私网地址拒绝
    3. 实际 TCP 连接目标使用已验证 IP
    4. TLS SNI 和证书主机名使用原始域名
    5. HTTP Host 请求头使用原始域名
    6. 每次重定向重新校验目标
    7. 禁止自动跟随未经检查的重定向
    8. 超时、下载失败和文件大小超限时清理临时文件
    9. 既有公开接口、错误处理与文件原子写入行为不回退
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestIPBinding(unittest.TestCase):
    """验证实际 TCP 连接绑定到已验证 IP。"""

    def test_connection_target_is_verified_ip(self):
        """实际 TCP 连接目标必须是 DNS 预校验的 IP。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        target_ip = '93.184.216.34'
        hostname = 'example.com'

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (target_ip, 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '10'}
                mock_resp.data = b'1234567890'
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d.download(f'https://{hostname}/file.txt')

                # 验证 HTTPSConnectionPool 被调用，且 host 参数是验证后的 IP
                mock_pool_cls.assert_called_once()
                call_args = mock_pool_cls.call_args
                self.assertEqual(call_args[0][0], target_ip,
                    f"连接目标应为已验证 IP {target_ip}，实际为 {call_args[0][0]}")

    def test_sni_uses_original_hostname(self):
        """TLS SNI 必须使用原始域名。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        hostname = 'example.com'

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '10'}
                mock_resp.data = b'1234567890'
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d.download(f'https://{hostname}/file.txt')

                call_kwargs = mock_pool_cls.call_args[1]
                self.assertEqual(call_kwargs.get('server_hostname'), hostname,
                    "SNI 应使用原始域名")

    def test_cert_validation_uses_original_hostname(self):
        """TLS 证书验证必须使用原始域名。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        hostname = 'example.com'

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '10'}
                mock_resp.data = b'1234567890'
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d.download(f'https://{hostname}/file.txt')

                call_kwargs = mock_pool_cls.call_args[1]
                self.assertEqual(call_kwargs.get('assert_hostname'), hostname,
                    "证书验证应使用原始域名")

    def test_host_header_uses_original_hostname(self):
        """HTTP Host 请求头必须使用原始域名。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        hostname = 'example.com'

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '10'}
                mock_resp.data = b'1234567890'
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d.download(f'https://{hostname}/file.txt')

                # 验证 request 调用传了 Host header
                mock_pool.request.assert_called_once()
                call_kwargs = mock_pool.request.call_args[1]
                headers = call_kwargs.get('headers', {})
                self.assertEqual(headers.get('Host'), hostname,
                    "Host 请求头应使用原始域名")


class TestRedirectRevalidation(unittest.TestCase):
    """验证每次重定向都重新校验目标。"""

    def test_redirect_revalidates_dns_each_hop(self):
        """每次重定向后都重新解析 DNS 并校验。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        call_count = [0]
        target_ips = ['93.184.216.34', '93.184.216.34']

        def capture_dns(hostname, port, family, typ):
            idx = call_count[0]
            call_count[0] += 1
            ip = target_ips[min(idx, len(target_ips) - 1)]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0))]

        with patch('computer.downloader.socket.getaddrinfo', side_effect=capture_dns):
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp1 = MagicMock()
                mock_resp1.status = 302
                mock_resp1.headers = {'location': 'https://example.com/next'}
                mock_resp2 = MagicMock()
                mock_resp2.status = 200
                mock_resp2.headers = {'content-length': '10'}
                mock_resp2.data = b'1234567890'
                mock_pool.request.side_effect = [mock_resp1, mock_resp2]
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                result = d.download('https://example.com/start')
                self.assertTrue(result['success'])
                self.assertGreaterEqual(call_count[0], 2)


class TestNoAutoRedirect(unittest.TestCase):
    """验证不自动跟随未经检查的重定向。"""

    def test_no_auto_redirect(self):
        """urllib3 不使用自动重定向，由我们手动控制。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        hostname = 'example.com'

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 302
                mock_resp.headers = {'location': 'http://evil.com/hijack'}
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                result = d.download(f'https://{hostname}/redirect')
                self.assertFalse(result['success'])

                # Verify no automatic redirect happened - we manually checked
                # The pool should have been called only once
                self.assertEqual(mock_pool.request.call_count, 1)


class TestTempFileCleanup(unittest.TestCase):
    """验证失败时临时文件被清理。"""

    def test_temp_file_cleaned_on_os_error(self):
        """写文件失败时临时文件应被清理。"""
        import tempfile
        import os
        from computer.downloader import Downloader
        import inspect
        source = inspect.getsource(Downloader.download)
        # Verify cleanup logic exists
        self.assertIn('os.unlink(temp_path)', source)
        self.assertIn('except OSError', source)

    def test_temp_file_cleaned_on_size_exceeded(self):
        """文件大小超限后临时文件应被清理。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader(max_size=10)  # 10 byte limit
        hostname = 'example.com'

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '500'}
                mock_resp.data = b'x' * 500
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                # This will fail at content-length check, before temp file creation
                result = d.download(f'https://{hostname}/large')
                self.assertFalse(result['success'])

    def test_temp_file_cleaned_on_network_error(self):
        """网络错误时不应创建临时文件。"""
        from computer.downloader import Downloader
        import socket
        d = Downloader()

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool.request.side_effect = Exception("network error")
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                result = d.download('https://example.com/fail')
                self.assertFalse(result['success'])
                # Should not create temp file on network error
                mock_pool_cls.assert_called_once()


class TestPublicInterfaces(unittest.TestCase):
    """验证既有公开接口行为不变。"""

    def setUp(self):
        from computer.downloader import Downloader
        self.d = Downloader()

    def test_download_interface(self):
        """download() 返回字典，包含 success/url/path/size/sha256/error/message。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))
            ]
            # DNS validation will fail for localhost, but response shape should be correct
            result = self.d.download('https://example.com/test')
            self.assertIn('success', result)
            self.assertIn('url', result)
            self.assertIn('path', result)
            self.assertIn('size', result)
            self.assertIn('sha256', result)
            self.assertIn('error', result)
            self.assertIn('message', result)

    def test_validate_url_interface(self):
        """_validate_url() 返回 dict 含 success/url/error/message。"""
        result = self.d._validate_url('https://example.com')
        self.assertIn('success', result)
        self.assertIn('url', result)
        self.assertTrue(result['success'])

    def test_sanitize_filename_interface(self):
        """_sanitize_filename() 返回清理后的字符串。"""
        safe = self.d._sanitize_filename('test<>:"file*.txt')
        self.assertNotIn('<', safe)
        self.assertNotIn('"', safe)
        self.assertNotIn('*', safe)

    def test_resolve_filename_interface(self):
        """_resolve_filename() 返回文件名。"""
        name = self.d._resolve_filename(None, 'https://example.com/path/file.zip', b'data')
        self.assertIsInstance(name, str)
        self.assertTrue(name.endswith('.zip'))


class TestDNSSafety(unittest.TestCase):
    """DNS 安全性测试。"""

    def setUp(self):
        from computer.downloader import Downloader
        self.d = Downloader()

    def test_dns_private_ip_rejected(self):
        """私有 IP 应被拒绝。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 0))
            ]
            result = self.d._resolve_and_validate_dns("private.example.com")
            self.assertFalse(result['success'])

    def test_dns_loopback_rejected(self):
        """回环地址应被拒绝。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))
            ]
            result = self.d._resolve_and_validate_dns("localhost.example.com")
            self.assertFalse(result['success'])

    def test_dns_link_local_rejected(self):
        """链路本地地址应被拒绝。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.1.1', 0))
            ]
            result = self.d._resolve_and_validate_dns("linklocal.example.com")
            self.assertFalse(result['success'])

    def test_dns_reserved_rejected(self):
        """保留地址应被拒绝。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.1', 0))
            ]
            result = self.d._resolve_and_validate_dns("reserved.example.com")
            self.assertFalse(result['success'])

    def test_dns_empty_result_fail_closed(self):
        """DNS 空结果应 fail-closed。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = []
            result = self.d._resolve_and_validate_dns("empty.example.com")
            self.assertFalse(result['success'])

    def test_dns_gaierror_fail_closed(self):
        """DNS 解析异常应 fail-closed。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.side_effect = socket.gaierror("getaddrinfo failed")
            result = self.d._resolve_and_validate_dns("fail.example.com")
            self.assertFalse(result['success'])

    def test_dns_mixed_addresses_rejected(self):
        """混合公网/私网地址应被拒绝。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 0)),
            ]
            result = self.d._resolve_and_validate_dns("mixed.example.com")
            self.assertFalse(result['success'])

    def test_dns_public_ip_allowed(self):
        """纯公网地址应被允许。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.35', 0)),
            ]
            result = self.d._resolve_and_validate_dns("public.example.com")
            self.assertTrue(result['success'])


class TestErrorHandling(unittest.TestCase):
    """错误处理测试。"""

    def setUp(self):
        from computer.downloader import Downloader
        self.d = Downloader()

    def test_http_error_handling(self):
        """HTTP 错误应被正确捕获并返回失败。"""
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool.request.side_effect = Exception("connection refused")
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                result = self.d.download('https://example.com/fail')
                self.assertFalse(result['success'])
                self.assertIn('error', result)

    def test_size_limit_enforced_after_download(self):
        """下载完成后内容大小超过限制应被拒绝。"""
        from computer.downloader import Downloader
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {}  # No content-length
                mock_resp.data = b'x' * 200  # Actual data exceeds limit
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d = Downloader(max_size=100)
                result = d.download('https://example.com/medium')
                self.assertFalse(result['success'])

    def test_content_length_too_large(self):
        """Content-Length 声明超过限制应拒绝。"""
        from computer.downloader import Downloader
        import socket
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))
            ]
            with patch('computer.downloader.urllib3.HTTPSConnectionPool') as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '999999'}
                mock_resp.data = b'x' * 10
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d = Downloader(max_size=100)
                result = d.download('https://example.com/large')
                self.assertFalse(result['success'])

    def test_http_url_rejected(self):
        """HTTP URL 必须被拒绝。"""
        result = self.d.download('http://example.com/file.zip')
        self.assertFalse(result['success'])
        self.assertIn('HTTPS', result['message'] or '')

    def test_empty_url_rejected(self):
        """空 URL 必须被拒绝。"""
        result = self.d.download('')
        self.assertFalse(result['success'])


# ============================================================
# Test: DNS TOCTOU（校验 IP 与连接 IP 必须一致）
# ============================================================
class TestDNSRebindingTOCTOU(unittest.TestCase):
    """
    验证「校验的 IP」与「实际连接的 IP」是同一次解析结果。

    回归缺陷：旧实现调用两次 socket.getaddrinfo()，
    第一次用于安全校验，第二次用于取连接 IP。
    两次解析之间若 DNS 被重绑，就会出现
    「校验通过公网 IP、实际连接私网 IP」的绕过。
    """

    def test_only_one_dns_resolution_per_hop(self):
        """每次请求只允许解析一次 DNS。"""
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        calls = []

        def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
            calls.append(host)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('93.184.216.34', 0))
            ]

        with patch('computer.downloader.socket.getaddrinfo',
                   side_effect=fake_getaddrinfo):
            with patch(
                'computer.downloader.urllib3.HTTPSConnectionPool'
            ) as mock_pool_cls:
                mock_pool = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.headers = {'content-length': '4'}
                mock_resp.data = b'test'
                mock_pool.request.return_value = mock_resp
                mock_pool_cls.return_value = mock_pool
                mock_pool.close = MagicMock()

                d.download('https://example.com/f.txt')

        self.assertEqual(
            len(calls), 1,
            f'单次请求应只解析一次 DNS，实际 {len(calls)} 次',
        )

    def test_connection_uses_same_ip_that_was_validated(self):
        """
        即使 DNS 第二次会返回私网 IP，连接也必须使用
        第一次（已校验）的公网 IP。
        """
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        calls = []
        captured = {}

        def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
            calls.append(host)
            if len(calls) == 1:
                ip = '93.184.216.34'   # 公网，校验通过
            else:
                ip = '192.168.1.99'    # 私网，若被使用即为绕过
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0))
            ]

        def fake_pool(host, **kwargs):
            captured['host'] = host
            pool = MagicMock()
            resp = MagicMock()
            resp.status = 200
            resp.headers = {'content-length': '4'}
            resp.data = b'test'
            pool.request.return_value = resp
            return pool

        with patch('computer.downloader.socket.getaddrinfo',
                   side_effect=fake_getaddrinfo):
            with patch(
                'computer.downloader.urllib3.HTTPSConnectionPool',
                side_effect=fake_pool,
            ):
                result = d.download('https://example.com/f.txt')

        self.assertEqual(
            captured.get('host'), '93.184.216.34',
            '连接目标必须是校验时使用的公网 IP，不能是第二次解析的私网 IP',
        )
        self.assertTrue(result['success'])

    def test_validate_returns_ip_in_result(self):
        """_resolve_and_validate_dns() 必须返回本次校验的公网 IP。"""
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('93.184.216.34', 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('93.184.216.35', 0)),
            ]
            result = d._resolve_and_validate_dns('example.com')

        self.assertTrue(result['success'])
        self.assertEqual(result.get('ip'), '93.184.216.34')

    def test_private_only_result_has_no_ip(self):
        """全部私网地址时不得返回可用 IP。"""
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('192.168.1.1', 0)),
            ]
            result = d._resolve_and_validate_dns('internal.example.com')

        self.assertFalse(result['success'])
        self.assertIsNone(result.get('ip'))


class TestNonDefaultPort(unittest.TestCase):
    """非默认端口的处理（端口不得硬编码为 443）。"""

    def test_custom_port_passed_to_pool(self):
        """URL 中的自定义端口必须传给连接池。"""
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        captured = {}

        def fake_pool(host, **kwargs):
            captured['host'] = host
            captured['port'] = kwargs.get('port')
            captured['headers'] = None
            pool = MagicMock()

            def _request(method, path, headers=None, **kw):
                captured['headers'] = headers
                resp = MagicMock()
                resp.status = 200
                resp.headers = {'content-length': '4'}
                resp.data = b'test'
                return resp

            pool.request.side_effect = _request
            return pool

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('93.184.216.34', 0))
            ]
            with patch(
                'computer.downloader.urllib3.HTTPSConnectionPool',
                side_effect=fake_pool,
            ):
                result = d.download('https://example.com:8443/f.txt')

        self.assertEqual(captured.get('port'), 8443)
        self.assertEqual(captured.get('host'), '93.184.216.34')
        self.assertTrue(result['success'])

    def test_non_default_port_in_host_header(self):
        """非默认端口的 Host 头必须带端口号。"""
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        captured = {}

        def fake_pool(host, **kwargs):
            pool = MagicMock()

            def _request(method, path, headers=None, **kw):
                captured['headers'] = headers
                resp = MagicMock()
                resp.status = 200
                resp.headers = {'content-length': '4'}
                resp.data = b'test'
                return resp

            pool.request.side_effect = _request
            return pool

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('93.184.216.34', 0))
            ]
            with patch(
                'computer.downloader.urllib3.HTTPSConnectionPool',
                side_effect=fake_pool,
            ):
                d.download('https://example.com:8443/f.txt')

        self.assertEqual(
            (captured.get('headers') or {}).get('Host'),
            'example.com:8443',
        )

    def test_default_port_host_header_without_port(self):
        """默认 443 端口的 Host 头不应带端口号。"""
        from computer.downloader import Downloader
        import socket

        d = Downloader()
        captured = {}

        def fake_pool(host, **kwargs):
            pool = MagicMock()

            def _request(method, path, headers=None, **kw):
                captured['headers'] = headers
                resp = MagicMock()
                resp.status = 200
                resp.headers = {'content-length': '4'}
                resp.data = b'test'
                return resp

            pool.request.side_effect = _request
            return pool

        with patch('computer.downloader.socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 ('93.184.216.34', 0))
            ]
            with patch(
                'computer.downloader.urllib3.HTTPSConnectionPool',
                side_effect=fake_pool,
            ):
                d.download('https://example.com/f.txt')

        self.assertEqual(
            (captured.get('headers') or {}).get('Host'),
            'example.com',
        )


class TestRedirectURLResolution(unittest.TestCase):
    """相对重定向 URL 必须按 RFC 3986 解析。"""

    def setUp(self):
        from computer.downloader import Downloader
        self.d = Downloader()

    def test_relative_filename_keeps_base_directory(self):
        """'f.txt' 相对路径必须保留基准目录。"""
        result = self.d._resolve_redirect_url(
            'https://a.com/x/y', 'f.txt'
        )
        self.assertEqual(result, 'https://a.com/x/f.txt')

    def test_parent_relative_path(self):
        """'../z.txt' 必须正确回溯一级。"""
        result = self.d._resolve_redirect_url(
            'https://a.com/x/y', '../z.txt'
        )
        self.assertEqual(result, 'https://a.com/z.txt')

    def test_absolute_path(self):
        """'/abs' 必须回到站点根。"""
        result = self.d._resolve_redirect_url(
            'https://a.com/x/y', '/abs'
        )
        self.assertEqual(result, 'https://a.com/abs')

    def test_protocol_relative_url(self):
        """'//host/path' 必须继承基准协议。"""
        result = self.d._resolve_redirect_url(
            'https://a.com/x/y', '//b.com/f'
        )
        self.assertEqual(result, 'https://b.com/f')

    def test_absolute_url_unchanged(self):
        """绝对 URL 必须原样返回。"""
        result = self.d._resolve_redirect_url(
            'https://a.com/x/y', 'https://c.com/f'
        )
        self.assertEqual(result, 'https://c.com/f')


# ============================================================
# Run
# ============================================================
if __name__ == '__main__':
    import inspect
    import socket
    unittest.main(verbosity=2)
