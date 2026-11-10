
"""AronaClient V0.2 — 真实传输层验收测试（本地受控 HTTPS 服务器）。

与 test_ip_binding.py 的区别（重要）：
    test_ip_binding.py 使用 mock 验证「调用参数是否正确」。
    本文件启动**真实的本地 HTTPS 服务器**，走真实的
    TCP 连接 + TLS 握手 + 证书校验 + HTTP 响应 + 文件落盘，
    用于验证 mock 无法证明的行为。

测试隔离：
    * 只用 127.0.0.1 本地回环，不访问公网。
    * 用 trustme 生成临时 CA/证书，不触碰系统证书库。
    * 用 patch 替换 _resolve_and_validate_dns()，让本地回环通过
      DNS 安全门禁 —— 这是**测试专用**隔离，生产代码的
      DNS 安全策略未做任何放宽。
    * 不修改系统 DNS，不修改 hosts 文件。

安全拒绝路径（私有/回环/混合地址等）由 test_ip_binding.py
单独覆盖，两者不可混淆。
"""

import hashlib
import http.server
import shutil
import ssl
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import trustme
    import urllib3
    HAS_TLS_TEST_DEPS = True
except ImportError:  # pragma: no cover
    HAS_TLS_TEST_DEPS = False

import computer.downloader as dl_mod
from computer.downloader import Downloader


HOSTNAME = "test.example.com"
BODY = b"ARONA-REAL-TRANSPORT-PAYLOAD-" * 20
SMALL_BODY = b"SMALL-OK"


class _Server:
    """本地受控 HTTPS 服务器（127.0.0.1，随机端口，临时证书）。

    参数：
        expired: 为 True 时签发**已过期**的证书（not_after 在过去），
                 用于验证 TLS 有效期校验真实生效。
    """

    def __init__(self, tmp_dir: Path, expired: bool = False):
        self.tmp_dir = tmp_dir
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        ca = trustme.CA()

        if expired:
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            cert = ca.issue_cert(
                HOSTNAME,
                not_before=now - datetime.timedelta(days=30),
                not_after=now - datetime.timedelta(days=1),
            )
        else:
            cert = ca.issue_cert(HOSTNAME)

        self.cert_path = self.tmp_dir / "cert.pem"
        self.key_path = self.tmp_dir / "key.pem"
        self.ca_path = self.tmp_dir / "ca.pem"

        cert.cert_chain_pems[0].write_to_path(str(self.cert_path))
        cert.private_key_pem.write_to_path(str(self.key_path))
        ca.cert_pem.write_to_path(str(self.ca_path))

        self.routes = {}
        self.hits = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                entry = outer.routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, headers, body = entry
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.cert_path), str(self.key_path))
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(
            target=self.srv.serve_forever, daemon=True
        )
        self._thread.start()

    def url(self, path):
        return f"https://{HOSTNAME}:{self.port}{path}"

    def shutdown(self):
        self.srv.shutdown()
        self.srv.server_close()


@unittest.skipUnless(
    HAS_TLS_TEST_DEPS, "需要 trustme 与 urllib3 才能运行真实传输测试"
)
class TestRealTransport(unittest.TestCase):
    """真实 TCP + TLS + 落盘的下载成功路径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = PROJECT_ROOT / "tests" / "_tmp_transport"
        cls.server = _Server(cls.tmp)
        cls.download_dir = cls.tmp / "downloads"
        cls.download_dir.mkdir(parents=True, exist_ok=True)

        cls.server.routes = {
            "/file.bin": (200, {}, BODY),
            "/small.bin": (200, {}, SMALL_BODY),
            "/big.bin": (200, {}, b"x" * 5000),
            "/dir/start": (302, {"Location": "next.bin"}, b""),
            "/dir/next.bin": (200, {}, b"RELATIVE-REDIRECT-OK"),
            "/redir-abs": (
                302,
                {"Location": cls.server.url("/small.bin")},
                b"",
            ),
            "/redir-http": (
                302,
                {"Location": "http://insecure.example.com/x"},
                b"",
            ),
            "/redir-loop": (302, {"Location": "/redir-loop"}, b""),
        }

        cls._orig_pool = urllib3.HTTPSConnectionPool

        def patched_pool(*args, **kwargs):
            kwargs.setdefault("ca_certs", str(cls.server.ca_path))
            kwargs.setdefault("cert_reqs", "CERT_REQUIRED")
            return cls._orig_pool(*args, **kwargs)

        urllib3.HTTPSConnectionPool = patched_pool

    @classmethod
    def tearDownClass(cls):
        urllib3.HTTPSConnectionPool = cls._orig_pool
        cls.server.shutdown()
        for p in cls.download_dir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        # 清理测试临时目录，避免污染工作区
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _downloader(self, **kwargs):
        return Downloader(download_root=self.download_dir, **kwargs)

    def _local_dns(self, hostname):
        """测试专用：让本地回环通过 DNS 门禁。"""
        return {"success": True, "ip": "127.0.0.1"}

    # ---------------- 成功路径 ----------------

    def test_real_download_success(self):
        """真实 HTTPS 下载：连接、TLS、内容、SHA-256、落盘。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/file.bin"))

        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["size"], len(BODY))
        self.assertEqual(
            result["sha256"], hashlib.sha256(BODY).hexdigest()
        )

        saved = Path(result["path"])
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), BODY)
        saved.unlink()

    def test_real_custom_port_used(self):
        """自定义端口真实连通（非 443）。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/small.bin"))

        self.assertTrue(result["success"], result.get("error"))
        self.assertNotEqual(self.server.port, 443)
        Path(result["path"]).unlink()

    # ---------------- 重定向 ----------------

    def test_real_absolute_redirect(self):
        """绝对 URL 重定向真实跟随成功。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/redir-abs"))

        self.assertTrue(result["success"], result.get("error"))
        self.assertIn("/small.bin", result["url"])
        Path(result["path"]).unlink()

    def test_real_relative_redirect_keeps_directory(self):
        """相对路径重定向保留基准目录（/dir/start -> /dir/next.bin）。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/dir/start"))

        self.assertTrue(result["success"], result.get("error"))
        self.assertIn("/dir/next.bin", result["url"])
        saved = Path(result["path"])
        self.assertEqual(saved.read_bytes(), b"RELATIVE-REDIRECT-OK")
        saved.unlink()

    def test_no_auto_redirect_by_urllib3(self):
        """
        urllib3 不得自动跟随重定向（必须由下载器逐跳校验）。

        回归缺陷：urllib3 默认 retries=Retry(3)，Retry 默认
        redirect=True，会自动跟随 3xx，从而完全绕过下载器的
        手动校验。修复为 retries=False。
        """
        self.server.hits.clear()
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/redir-abs"))

        self.assertTrue(result["success"], result.get("error"))
        # 两次请求都应经过下载器：/redir-abs 和 /small.bin
        self.assertIn("/redir-abs", self.server.hits)
        self.assertIn("/small.bin", self.server.hits)
        Path(result["path"]).unlink()

    def test_redirect_to_http_blocked(self):
        """重定向到 HTTP 必须被拒绝。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/redir-http"))

        self.assertFalse(result["success"])
        self.assertIn("HTTPS", (result["error"] or "").upper()
                      + (result["message"] or "").upper())

    def test_redirect_loop_blocked(self):
        """循环重定向必须被检测。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/redir-loop"))

        self.assertFalse(result["success"])

    # ---------------- TLS 失败关闭 ----------------

    def test_untrusted_certificate_fails_closed(self):
        """不受信任的证书必须失败关闭。"""
        d = self._downloader()
        orig = urllib3.HTTPSConnectionPool
        urllib3.HTTPSConnectionPool = self._orig_pool  # 不注入测试 CA
        try:
            with patch.object(
                d, "_resolve_and_validate_dns",
                side_effect=self._local_dns,
            ):
                result = d.download(self.server.url("/small.bin"))
        finally:
            urllib3.HTTPSConnectionPool = orig

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])

    def test_hostname_mismatch_fails_closed(self):
        """证书主机名不匹配必须失败关闭。"""
        d = self._downloader()
        with patch.object(
            d, "_resolve_and_validate_dns",
            side_effect=lambda h: {"success": True, "ip": "127.0.0.1"},
        ):
            result = d.download(
                f"https://wrong.example.org:{self.server.port}/small.bin"
            )

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])

    # ---------------- 限制与清理 ----------------

    def test_size_limit_rejects_and_leaves_no_temp(self):
        """响应体超限时拒绝，且不残留临时文件。"""
        d = self._downloader(max_size=100)
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/big.bin"))

        self.assertFalse(result["success"])
        self.assertEqual(list(self.download_dir.glob("*.tmp")), [])

    def test_http_404_fails_without_writing(self):
        """404 时失败且不写文件。"""
        d = self._downloader()
        before = set(self.download_dir.glob("*"))
        with patch.object(
            d, "_resolve_and_validate_dns", side_effect=self._local_dns
        ):
            result = d.download(self.server.url("/missing"))

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertEqual(set(self.download_dir.glob("*")), before)


@unittest.skipUnless(
    HAS_TLS_TEST_DEPS, "需要 trustme 与 urllib3 才能运行真实传输测试"
)
class TestExpiredCertificate(unittest.TestCase):
    """过期 TLS 证书必须被拒绝（真实握手，非 mock）。

    证书由 trustme 签发、主机名匹配，但 not_after 已在过去。
    验证点：
        * 真实 TLS 握手因证书过期失败（错误信息含 expired）
        * 下载器不把该请求报告为成功
        * 不留下任何正式下载文件或临时文件
        * 安全校验（DNS/URL）未被绕过
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = PROJECT_ROOT / "tests" / "_tmp_expired_cert"
        cls.server = _Server(cls.tmp, expired=True)
        cls.download_dir = cls.tmp / "downloads"
        cls.download_dir.mkdir(parents=True, exist_ok=True)
        cls.server.routes = {"/file.bin": (200, {}, b"MUST-NOT-DELIVER")}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _local_dns(self, hostname):
        return {"success": True, "ip": "127.0.0.1"}

    def test_real_tls_handshake_rejects_expired_cert(self):
        """真实 TLS 握手因证书过期而失败。"""
        pool = urllib3.HTTPSConnectionPool(
            "127.0.0.1",
            port=self.server.port,
            assert_hostname=HOSTNAME,
            server_hostname=HOSTNAME,
            ca_certs=str(self.server.ca_path),
            cert_reqs="CERT_REQUIRED",
            timeout=5.0,
            retries=False,
        )
        try:
            with self.assertRaises(urllib3.exceptions.SSLError) as ctx:
                pool.request(
                    "GET", "/file.bin",
                    headers={"Host": HOSTNAME},
                    retries=False,
                )
            message = str(ctx.exception).lower()
            self.assertIn(
                "expired", message,
                f"失败原因应为证书过期，实际：{message}",
            )
        finally:
            pool.close()

    def test_downloader_rejects_expired_cert(self):
        """下载器对过期证书返回失败。"""
        orig = urllib3.HTTPSConnectionPool

        def patched(*args, **kwargs):
            kwargs.setdefault("ca_certs", str(self.server.ca_path))
            kwargs.setdefault("cert_reqs", "CERT_REQUIRED")
            return orig(*args, **kwargs)

        urllib3.HTTPSConnectionPool = patched
        try:
            d = Downloader(download_root=self.download_dir)
            with patch.object(
                d, "_resolve_and_validate_dns", side_effect=self._local_dns
            ):
                result = d.download(self.server.url("/file.bin"))
        finally:
            urllib3.HTTPSConnectionPool = orig

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertIsNone(result["size"])
        self.assertIsNone(result["sha256"])

    def test_expired_cert_leaves_no_file(self):
        """过期证书失败后不残留任何文件。"""
        orig = urllib3.HTTPSConnectionPool

        def patched(*args, **kwargs):
            kwargs.setdefault("ca_certs", str(self.server.ca_path))
            kwargs.setdefault("cert_reqs", "CERT_REQUIRED")
            return orig(*args, **kwargs)

        urllib3.HTTPSConnectionPool = patched
        try:
            d = Downloader(download_root=self.download_dir)
            before = set(self.download_dir.glob("*"))
            with patch.object(
                d, "_resolve_and_validate_dns", side_effect=self._local_dns
            ):
                d.download(self.server.url("/file.bin"))
            after = set(self.download_dir.glob("*"))
        finally:
            urllib3.HTTPSConnectionPool = orig

        self.assertEqual(before, after, "失败时不得写入任何文件")
        self.assertEqual(list(self.download_dir.glob("*.tmp")), [])

    def test_expired_cert_does_not_bypass_dns_checks(self):
        """过期证书路径不会绕过 DNS 安全校验。"""
        d = Downloader(download_root=self.download_dir)
        # 不 patch DNS：expired.example.com 无法解析为公网地址，应被拒绝
        result = d.download(self.server.url("/file.bin"))
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
