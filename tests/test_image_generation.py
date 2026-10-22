"""
R12.3-07 M5：图片生成离线测试 + 安全回归

覆盖（任务 §五）：
  - 门禁与默认关闭（G1）
  - 白名单 / 未知协议 / 未注册服务商（G3）
  - 凭据缺失（G2）
  - DNS 候选 / 公网校验（G4-4/G4-8）
  - 自定义 backend peer 校验 + TLS + 钩子组合（G5，回环 TLS Mock）
  - backend 构造期断言 / 失败无静默回退（G6）
  - 密钥不落盘 / 不进异常（S16/G2）
  - 结果保存：UUID / 大小上限 / 魔数 / Pillow 解码（G8）
  - 失败时零真实网络（G4/G1）

全程离线：合成凭据、Mock transport、回环 TLS、本地临时目录。
不触碰真实 OpenAI 端点 / 公网 / 真实 Key。
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import threading
import tempfile
import unittest
from pathlib import Path

import httpcore2
import httpx2

from PIL import Image
from io import BytesIO

import core.image_generation as ig
from core.image_generation import (
    ImageGenerationService,
    ImageGenError,
    PeerPolicyBackend,
    build_secure_client,
    install_security_hook,
    resolve_dns_candidates,
    validate_candidates,
    save_image_result,
    cleanup_partial,
    _is_public_ip,
    _sniff_image_format,
)
from core.settings import SettingsManager

SYNTH_KEY = "sk-test-not-a-real-secret"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _settings_factory(tmp: Path, enabled=False, with_key=True,
                      provider="openai", protocol="openai-images",
                      base_url="https://api.openai.com/v1"):
    """Build an isolated SettingsManager writing to `tmp/settings.json`."""
    m = SettingsManager
    old_dir, old_file = m.SETTINGS_DIR, m.SETTINGS_FILE
    m.SETTINGS_DIR = tmp
    m.SETTINGS_FILE = tmp / "settings.json"
    try:
        s = m()
        if enabled:
            s.set_image_generation_enabled(True)
        s.settings["image_generation"]["provider"] = provider
        s.set_image_provider_config(
            "openai",
            {"protocol": protocol, "base_url": base_url},
        )
        if with_key:
            s.settings["image_generation"]["provider"] = provider
            # write key only via in-memory override to avoid touching the
            # real keyring; the service reads via s.get_api_key which we
            # monkeypatch below when needed.
        yield_holder = s
    finally:
        m.SETTINGS_DIR, m.SETTINGS_FILE = old_dir, old_file
    return yield_holder


class _FakeKeyringSettings:
    """SettingsManager stand-in: keyring path is faked so tests never touch
    the real Windows Credential Manager (G2 compliance, offline).

    未注册的服务商（如 'bogus'）会触发 SettingsManager 白名单逻辑，
    返回 None；已知白名单内服务商按当前 provider 配置返回。
    """

    _WHITELIST = {"openai"}  # 与 SettingsManager.IMAGE_PROVIDER_WHITELIST 对齐

    def __init__(self, enabled=False, with_key=True, provider="openai",
                 protocol="openai-images",
                 base_url="https://api.openai.com/v1"):
        self._key = SYNTH_KEY if with_key else ""
        self._enabled = enabled
        self._provider = provider
        self._protocol = protocol
        self._base_url = base_url

    def get_image_generation_section(self):
        return {
            "enabled": self._enabled,
            "provider": self._provider,
            "providers": {
                self._provider: {
                    "base_url": self._base_url,
                    "protocol": self._protocol,
                    "model": "gpt-image-1",
                }
            },
        }

    def get_image_provider(self, provider_id):
        provider_id = str(provider_id).strip().lower()
        if provider_id not in self._WHITELIST:
            return None
        if provider_id != self._provider:
            # 白名单内但非当前 provider：返回空配置（让 service 走默认）
            return None
        return {
            "base_url": self._base_url,
            "protocol": self._protocol,
            "model": "gpt-image-1",
        }

    def get_api_key(self, name):
        # keyring path faked: return the synthetic key for the image slot.
        if name.startswith("image_"):
            return self._key
        return ""

    def get_image_api_key(self):
        return self._key


def _resolver(return_v4, return_v6):
    def resolver(host, family):
        if family == socket.AF_INET:
            return list(return_v4)
        return list(return_v6)
    return resolver


# ---------------------------------------------------------------------------
# G3/G4: DNS + public IP
# ---------------------------------------------------------------------------

class TestDNSAndPublicIP(unittest.TestCase):

    def test_private_and_loopback_rejected(self):
        for bad in ("10.0.0.1", "192.168.1.5", "127.0.0.1",
                    "172.16.0.1", "169.254.1.1", "0.0.0.0",
                    "100.64.0.1", "100.127.255.255"):
            self.assertFalse(_is_public_ip(bad), bad)
        # IPv6 private/loopback
        self.assertFalse(_is_public_ip("::1"))
        self.assertFalse(_is_public_ip("fe80::1"))

    def test_public_allowed(self):
        self.assertTrue(_is_public_ip("8.8.8.8"))
        self.assertTrue(_is_public_ip("1.1.1.1"))
        self.assertTrue(_is_public_ip("2001:4860:4860::8888"))

    def test_validate_candidates_all_private_raises_unbound(self):
        with self.assertRaises(ImageGenError):
            validate_candidates({"ipv4": ["10.0.0.1"], "ipv6": []})

    def test_validate_candidates_keeps_only_public(self):
        pub = validate_candidates(
            {"ipv4": ["10.0.0.1", "8.8.8.8"],
             "ipv6": ["fe80::1", "2001:4860:4860::8888"]}
        )
        self.assertEqual(pub, {"8.8.8.8", "2001:4860:4860::8888"})

    def test_resolve_no_records_raises_dns_failed(self):
        with self.assertRaises(ImageGenError) as ctx:
            resolve_dns_candidates("x.example", _resolver([], []))
        self.assertEqual(ctx.exception.code, "DNS_RESOLUTION_FAILED")

    def test_resolve_dedup_and_order(self):
        out = resolve_dns_candidates(
            "x", _resolver(["1.1.1.1", "1.1.1.1", "2.2.2.2"], [])
        )
        self.assertEqual(out["ipv4"], ["1.1.1.1", "2.2.2.2"])


# ---------------------------------------------------------------------------
# G1: gate / default-off
# ---------------------------------------------------------------------------

class TestGate(unittest.TestCase):

    def test_default_off_zero_network(self):
        svc = ImageGenerationService(
            settings=_FakeKeyringSettings(enabled=False, with_key=True)
        )
        ok, code = svc.is_available()
        self.assertFalse(ok)
        self.assertEqual(code, "FEATURE_DISABLED")

    def test_credential_missing(self):
        svc = ImageGenerationService(
            settings=_FakeKeyringSettings(enabled=True, with_key=False)
        )
        ok, code = svc.is_available()
        self.assertFalse(ok)
        self.assertEqual(code, "CREDENTIAL_MISSING")

    def test_unknown_protocol_unsupported(self):
        svc = ImageGenerationService(
            settings=_FakeKeyringSettings(enabled=True, protocol="custom-x")
        )
        ok, code = svc.is_available()
        self.assertFalse(ok)
        self.assertEqual(code, "UNSUPPORTED_PROTOCOL")

    def test_unknown_domain_unbound(self):
        svc = ImageGenerationService(
            settings=_FakeKeyringSettings(
                enabled=True, base_url="https://evil.example/v1")
        )
        ok, code = svc.is_available()
        self.assertFalse(ok)
        self.assertEqual(code, "CONNECT_TARGET_UNBOUND")

    def test_unregistered_provider(self):
        svc = ImageGenerationService(
            settings=_FakeKeyringSettings(enabled=True, provider="bogus")
        )
        ok, code = svc.is_available()
        self.assertFalse(ok)
        self.assertEqual(code, "PROVIDER_NOT_REGISTERED")

    def test_generate_when_disabled_is_no_network(self):
        svc = ImageGenerationService(
            settings=_FakeKeyringSettings(enabled=False, with_key=True)
        )
        with self.assertRaises(ImageGenError) as ctx:
            svc.generate("anything")
        self.assertEqual(ctx.exception.code, "FEATURE_DISABLED")
        # no http client was built -> zero network
        self.assertIsNone(svc._client)


# ---------------------------------------------------------------------------
# G5: backend + hook + TLS (loopback mock, like R12.3-06 O3)
# ---------------------------------------------------------------------------

class _LoopbackTLSServer:
    """Minimal TLS server on 127.0.0.1 serving a fake OpenAI images response.
    Self-signed cert with SAN=api.openai.com. In-process, no external host."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self.cert_path, self.key_path = _gen_cert()
        if self.cert_path is None:
            self.sock.close()
            self.cert_path = None
            return
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        self.ctx = ctx
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self.stopped.is_set():
            try:
                raw, _ = self.sock.accept()
            except OSError:
                break
            try:
                self._handle(raw)
            except Exception:
                pass

    def _handle(self, raw):
        try:
            tls = self.ctx.wrap_socket(raw, server_side=True)
        except ssl.SSLError:
            raw.close()
            return
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                req += chunk
            # Fake OpenAI /v1/images/generations response (b64 JSON PNG).
            body_png = _png_bytes()
            b64 = base64.b64encode(body_png).decode("ascii")
            payload = json.dumps({
                "created": 0,
                "data": [{"b64_json": b64, "revised_prompt": ""}],
            }).encode("utf-8")
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                b"Connection: keep-alive\r\n\r\n" + payload
            )
            tls.sendall(resp)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                tls.close()
            except Exception:
                pass

    def stop(self):
        self.stopped.set()
        try:
            self.sock.close()
        except Exception:
            pass


def _gen_cert() -> tuple:
    """Generate a self-signed cert+key with SAN=api.openai.com.

    Returns (cert_path, key_path) written to a temp dir the caller keeps
    alive for the test duration. ssl.SSLContext needs file paths, not
    in-memory PEM. Falls back to (None, None) when cryptography is
    unavailable so the TLS case can skip cleanly.
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import datetime

        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "api.openai.com"),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("api.openai.com")]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        import tempfile
        _tmp = Path(tempfile.mkdtemp(prefix="imgen_cert_"))
        cert_path = _tmp / "cert.pem"
        key_path = _tmp / "key.pem"
        cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
        return str(cert_path), str(key_path)
    except Exception:
        return None, None


class TestBackendAndTLS(unittest.TestCase):
    """G5: peer policy + TLS host verification + hook injection (loopback)."""

    @classmethod
    def setUpClass(cls):
        cls.server = _LoopbackTLSServer()
        if cls.server.cert_path is None:
            raise unittest.SkipTest("cryptography unavailable; TLS case skipped")
        cls.server.thread.start()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None) is not None and \
                getattr(cls.server, "thread", None) is not None:
            cls.server.stop()
            cls.server.thread.join(timeout=2)

    def _client_for(self, verified_ips, sni="api.openai.com"):
        ctx = ssl.create_default_context(cafile=None)
        ctx.load_verify_locations(self.server.cert_path)
        return build_secure_client(sni, verified_ips, ctx)

    def test_compliant_path_returns_200_image(self):
        client = self._client_for({"127.0.0.1"})
        # route the request to the loopback port while keeping SNI=api.openai.com
        req = client.build_request(
            "GET", f"https://127.0.0.1:{self.server.port}/v1"
        )
        resp = client.send(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'"data"', resp.content)
        client.close()

    def test_peer_rejected_before_tls(self):
        client = self._client_for(set())  # empty verified set -> reject all
        req = client.build_request(
            "GET", f"https://127.0.0.1:{self.server.port}/v1"
        )
        try:
            client.send(req)
            raise AssertionError("expected PEER_REJECTED, got a response")
        except httpcore2.ConnectError as exc:
            self.assertIn("PEER_REJECTED", str(exc))
        client.close()

    def test_backend_type_asserted_at_construction(self):
        with self.assertRaises(TypeError):
            PeerPolicyBackend("not-a-set")

    def test_hook_missing_fails_closed(self):
        # A client whose _event_hooks dict lacks a "request" list must be
        # refused by the installer, not silently skipped (G6).
        from httpx2._transports.mock import MockTransport

        def _handler(request):
            return httpx2.Response(200, headers={})

        plain = httpx2.Client(transport=MockTransport(_handler))
        plain._event_hooks = {}
        with self.assertRaises(ImageGenError):
            install_security_hook(plain, "api.openai.com")

    def test_hook_injects_sni_and_host(self):
        client = self._client_for({"127.0.0.1"})
        # the installer must have registered at least one request hook
        self.assertTrue(client._event_hooks.get("request"))
        # run every hook against a synthetic request (pre-send point)
        req = client.build_request("GET", "https://127.0.0.1:443/v1")
        for hook in client._event_hooks["request"]:
            hook(req)
        self.assertEqual(req.extensions.get("sni_hostname"), "api.openai.com")
        self.assertEqual(req.headers.get("Host"), "api.openai.com")
        client.close()


# ---------------------------------------------------------------------------
# G8: result storage
# ---------------------------------------------------------------------------

class TestSaveResult(unittest.TestCase):

    def test_uuid_save_and_pillow_ok(self):
        with tempfile.TemporaryDirectory() as d:
            p = save_image_result(_png_bytes(), Path(d))
            self.assertTrue(p.exists())
            self.assertRegex(p.name, r"^img_[0-9a-f]{32}\.png$")
            with Image.open(p) as im:
                im.verify()

    def test_no_overwrite_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = save_image_result(_png_bytes(), Path(d))
            p2 = save_image_result(_png_bytes(), Path(d))
            self.assertNotEqual(p1, p2)

    def test_too_large_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            big = _png_bytes() + b"0" * (ig.IMAGE_MAX_OUTPUT_BYTES + 1)
            with self.assertRaises(ImageGenError) as ctx:
                save_image_result(big, Path(d))
            self.assertEqual(ctx.exception.code, "OUTPUT_TOO_LARGE")
            # nothing written
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_bad_magic_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ImageGenError) as ctx:
                save_image_result(b"not-an-image", Path(d))
            self.assertEqual(ctx.exception.code, "INVALID_IMAGE_RESPONSE")

    def test_corrupt_image_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            # valid PNG magic but truncated body -> Pillow decode fails
            truncated = _png_bytes()[: len(_png_bytes()) // 2]
            if _sniff_image_format(truncated) is None:
                # pad to keep magic so it reaches the Pillow stage
                truncated = _png_bytes()[:8] + b"\x00" * 64
            with self.assertRaises(ImageGenError) as ctx:
                save_image_result(truncated, Path(d))
            self.assertEqual(ctx.exception.code, "IMAGE_DECODE_FAILED")

    def test_cleanup_only_own_file(self):
        # cleanup_partial only touches files named img_<32hex> inside a
        # directory named "results" (G8: never user files). Mirror that
        # exact layout here so the guard actually exercises the delete.
        with tempfile.TemporaryDirectory() as d:
            results = Path(d) / "results"
            results.mkdir()
            p = save_image_result(_png_bytes(), results)
            user_file = results / "user.png"
            user_file.write_bytes(_png_bytes())
            cleanup_partial(p)
            self.assertFalse(p.exists())
            self.assertTrue(user_file.exists())


# ---------------------------------------------------------------------------
# G2/S16: key never in logs / exceptions / output
# ---------------------------------------------------------------------------

class TestKeyNotLeaked(unittest.TestCase):

    def test_exception_text_free_of_key(self):
        errors = [
            ImageGenError.feature_disabled(),
            ImageGenError.credential_missing("openai"),
            ImageGenError.connect_peer_rejected("openai"),
            ImageGenError.tls_verification_failed("openai"),
            ImageGenError.provider_error(503, "openai"),
        ]
        for e in errors:
            self.assertNotIn(SYNTH_KEY, str(e))
            self.assertNotIn(SYNTH_KEY[:8], str(e))
            self.assertNotIn(SYNTH_KEY[-8:], str(e))

    def test_from_exception_maps_without_key(self):
        exc = httpcore2.ConnectError("boom PEER_REJECTED")
        e = ImageGenError.from_exception(exc, "openai")
        self.assertEqual(e.code, "CONNECT_PEER_REJECTED")
        self.assertNotIn(SYNTH_KEY, str(e))


# ---------------------------------------------------------------------------
# M2 settings accessors (offline, isolated SETTINGS_DIR)
# ---------------------------------------------------------------------------

class TestSettingsAccessors(unittest.TestCase):

    def _manager(self, tmp: Path):
        m = SettingsManager
        old_dir, old_file = m.SETTINGS_DIR, m.SETTINGS_FILE
        m.SETTINGS_DIR = tmp
        m.SETTINGS_FILE = tmp / "settings.json"
        s = m()
        return s, (old_dir, old_file)

    def test_default_off_and_whitelist(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            s, _ = self._manager(tmp)
            sec = s.get_image_generation_section()
            self.assertIs(sec["enabled"], False)
            self.assertEqual(sec["provider"], "openai")
            self.assertIsNotNone(s.get_image_provider("openai"))
            self.assertIsNone(s.get_image_provider("bogus"))

    def test_enable_and_provider_config(self):
        with tempfile.TemporaryDirectory() as d:
            s, _ = self._manager(Path(d))
            s.set_image_generation_enabled(True)
            s.set_image_provider_config("openai", {"model": "gpt-image-1"})
            sec = s.get_image_generation_section()
            self.assertIs(sec["enabled"], True)
            self.assertEqual(sec["providers"]["openai"]["model"], "gpt-image-1")

    def test_unknown_provider_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            s, _ = self._manager(Path(d))
            with self.assertRaises(ValueError):
                s.set_image_provider_config("evil", {"base_url": "https://x"})

    def test_non_https_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            s, _ = self._manager(Path(d))
            with self.assertRaises(ValueError):
                s.set_image_provider_config(
                    "openai", {"base_url": "http://insecure.example"})

    def test_reload_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            s, _ = self._manager(Path(d))
            s.set_image_generation_enabled(True)
            # corrupt the file -> reload must fall back to default (off)
            Path(d, "settings.json").write_text("{not json", encoding="utf-8")
            s.reload()
            self.assertIs(s.get_image_generation_section()["enabled"], False)


# ---------------------------------------------------------------------------
# R12.3-08 F1：openai SDK base_url 处理
# ---------------------------------------------------------------------------

class TestOpenAIBaseURLHandling(unittest.TestCase):
    """generate() 中 openai 客户端不设置绝对 IP base_url，
    相对路径经 httpx2 base_url（默认 api.openai.com/v1）合并后
    与 SNI=api.openai.com 保持一致。"""

    def test_openai_client_uses_default_base_url_not_ip(self):
        # 模拟 generate() 中构造 openai 客户端的方式：
        # 不传 base_url -> openai 默认 "https://api.openai.com/v1"
        import openai
        # 用真实 httpx2.Client + 安全 transport（无网络，Mock 式）
        from httpx2._transports.mock import MockTransport
        def _handler(request):
            return httpx2.Response(200, headers={}, content=b"{}")
        real_httpx = httpx2.Client(transport=MockTransport(_handler))
        client = openai.OpenAI(
            api_key="sk-test-not-a-real-secret",
            http_client=real_httpx,
            timeout=120.0,
            max_retries=0,
        )
        # openai 内部 base_url 应是域名形态（与 SNI 一致）
        self.assertIn("api.openai.com", str(client.base_url))
        self.assertNotIn("127.0.0.1", str(client.base_url))
        real_httpx.close()

    def test_generate_does_not_set_ip_base_url(self):
        """R12.3-08 F1：若设绝对 IP base_url，httpx2._merge_url 对绝对
        URL 不应用 base_url，请求域名与 SNI 不一致 -> TLS 校验失败。
        此测试验证当前实现【不】这样做。"""
        import inspect
        src = inspect.getsource(ig.ImageGenerationService.generate)
        self.assertNotIn(
            "base_url=", src,
            "generate() must not set an absolute IP base_url "
            "(breaks SNI/Host invariant, see R12.3-08 F1)",
        )


# ---------------------------------------------------------------------------
# R12.3-10 F3：ui.py 属性遮蔽方法回归测试
# ---------------------------------------------------------------------------

class TestUIAttributeShadowing(unittest.TestCase):
    """R12.3-10 F3：ui.py 中方法 `_image_gen_service` 不得被同名实例
    属性遮蔽。若 `__init__` 设置 `self._image_gen_service = None`，
    后续 `self._image_gen_service()` 调用会因 None 不可调用而抛
    TypeError，导致图片生成功能永久不可用。

    修复：实例属性重命名为 `_image_gen_service_obj`。
    本测试通过 inspect 验证源文件中不存在遮蔽性赋值。
    """

    def test_no_shadowing_assignment_in_init(self):
        import inspect
        import ui
        src = inspect.getsource(ui.AronaUI.__init__)
        self.assertNotIn(
            "self._image_gen_service =", src,
            "ui.py __init__ must not assign to self._image_gen_service "
            "(shadows the method, see R12.3-10 F3)",
        )
        self.assertIn(
            "self._image_gen_service_obj =", src,
            "ui.py __init__ must use self._image_gen_service_obj "
            "(non-shadowing name, see R12.3-10 F3)",
        )

    def test_service_method_uses_obj_slot(self):
        import inspect
        import ui
        src = inspect.getsource(ui.AronaUI._image_gen_service)
        self.assertIn(
            "self._image_gen_service_obj", src,
            "_image_gen_service must read/write _image_gen_service_obj "
            "(not the shadowing _image_gen_service attribute)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
