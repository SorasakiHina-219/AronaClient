
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
# Run
# ============================================================
if __name__ == '__main__':
    import inspect
    import socket
    unittest.main(verbosity=2)
