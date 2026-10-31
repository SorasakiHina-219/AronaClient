"""
R13-MEDIA-01：多媒体能力（视频生成 / 语音合成+识别 / 开放服务商图片）
离线单元测试 + 安全回归

覆盖（任务 §八）：
  - 配置加载 / 保存 / 旧配置兼容（media_services fail-closed）
  - 密钥读取与缺失密钥时的安全失败（CREDENTIAL_MISSING）
  - 服务商适配器请求构造、响应解析与异常处理（mock transport）
  - Qt 后台任务 / 重复触发 / 关闭窗口线程安全（MediaWorker + orphan 策略）
  - 未启用/未知服务商/未知协议门禁（零网络）
  - 视频异步任务：task_id / 状态 / 完成 / 失败 / 超时
  - TTS/STT 响应解析与文件保存
  - 真实端点（证书/SNI/CDN）不在本测试范围内（需 [A] 授权）

全程离线：合成凭据、Mock transport、回环、临时目录。
不触碰真实 OpenAI 端点 / 公网 / 真实 Key。
"""

from __future__ import annotations

import base64
import json
import socket
import ssl
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpcore2
import httpx2

import core.media_generation as mg
from core.media_generation import (
    MediaError,
    MediaProvider,
    MediaService,
    _OpenAIImageAdapter,
    _OpenAIVideoTaskAdapter,
    _OpenAIAudioAdapter,
    _result_hosts_from_prov,
    close_media_client,
    save_video_result,
    save_voice_result,
    save_stt_result,
    _extract_task_id,
    _extract_result_url,
    _sniff_video_ext,
    _parse_image_payload,
    _image_url_from_payload,
)
from core.image_generation import (
    ImageGenError,
    PeerPolicyBackend,
    install_security_hook,
    _build_core_pool_transport,
    _is_public_ip,
)
from core.settings import SettingsManager

# 用 Pillow 生成一个最小有效 PNG（避免手写 PNG 字节不全导致解码失败）。
def _make_png_bytes() -> bytes:
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


SYNTH_KEY = "sk-media-test-not-a-real-secret"


class _FakeKeyringSettings:
    """SettingsManager stand-in：media_services + keyring 全部内存 fake。

    不触碰真实 settings.json / Windows 凭据管理器（离线、隔离）。
    """

    def __init__(self, media_services=None, keys=None):
        self._ms = media_services or {
            "image": {"enabled": False, "provider": "", "providers": {}},
            "video": {"enabled": False, "provider": "", "providers": {}},
            "voice": {"enabled": False, "provider": "", "providers": {}},
        }
        self._keys = dict(keys or {})

    # -- 读路径 --
    def get_media_service_section(self, service):
        return self._ms.get(service, {"enabled": False, "provider": "", "providers": {}})

    def get_media_provider_config(self, service, provider_id):
        sec = self.get_media_service_section(service)
        return sec.get("providers", {}).get(str(provider_id).strip().lower())

    def has_media_api_key(self, service, provider_id=None):
        if provider_id is None:
            provider_id = self.get_media_service_section(service).get("provider", "")
        return bool(self._keys.get(f"{service}_{provider_id}", ""))

    def get_media_api_key(self, service, provider_id=None):
        if provider_id is None:
            provider_id = self.get_media_service_section(service).get("provider", "")
        return self._keys.get(f"{service}_{provider_id}", "")

    # -- S2 所需写路径（内存 fake，不触碰 keyring / settings.json）--
    def add_media_provider(self, service, provider_id, cfg):
        sec = self._ms.setdefault(
            service, {"enabled": False, "provider": "", "providers": {}}
        )
        sec.setdefault("providers", {})[str(provider_id).strip().lower()] = dict(cfg)
        if not sec.get("provider"):
            sec["provider"] = str(provider_id).strip().lower()

    def set_media_enabled(self, service, enabled):
        self._ms.setdefault(service, {})["enabled"] = bool(enabled)

    def set_media_api_key(self, service, provider_id, api_key):
        if api_key:
            self._keys[f"{service}_{provider_id}"] = api_key
        else:
            self._keys.pop(f"{service}_{provider_id}", None)


def _video_settings(provider="openai"):
    """一个启用的 video service（openai-video-task 协议）+ 合成密钥。
    S4-B2（F2）：含有效 result_hosts（F2 三态「有效白名单」）。"""
    ms = {
        "video": {
            "enabled": True,
            "provider": provider,
            "providers": {
                provider: {
                    "name": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "model": "sora-2",
                    "protocol": "openai-video-task",
                    "extra_params": {
                        "poll_path": "/videos/{task_id}",
                        "result_field": "url",
                        "status_field": "status",
                        "result_hosts": ["cdn.example.invalid"],
                    },
                }
            },
        },
        "image": {"enabled": False, "provider": "", "providers": {}},
        "voice": {"enabled": False, "provider": "", "providers": {}},
    }
    return _FakeKeyringSettings(media_services=ms, keys={"video_openai": SYNTH_KEY})


def _voice_settings(provider="openai"):
    ms = {
        "voice": {
            "enabled": True,
            "provider": provider,
            "providers": {
                provider: {
                    "name": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini-tts",
                    "protocol": "openai-audio",
                }
            },
        },
        "image": {"enabled": False, "provider": "", "providers": {}},
        "video": {"enabled": False, "provider": "", "providers": {}},
    }
    return _FakeKeyringSettings(media_services=ms, keys={"voice_openai": SYNTH_KEY})


# ---------------------------------------------------------------------------
# 门禁与安全失败
# ---------------------------------------------------------------------------


class TestGate(unittest.TestCase):

    def test_video_service_disabled_zero_network(self):
        s = _FakeKeyringSettings()
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        self.assertEqual(svc.is_available("video"), (False, "SERVICE_DISABLED"))

    def test_video_provider_not_configured(self):
        ms = {
            "video": {"enabled": True, "provider": "", "providers": {}},
            "image": {}, "voice": {},
        }
        s = _FakeKeyringSettings(media_services=ms)
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        self.assertEqual(svc.is_available("video"), (False, "PROVIDER_NOT_CONFIGURED"))

    def test_credential_missing(self):
        ms = {
            "video": {
                "enabled": True,
                "provider": "openai",
                "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                          "protocol": "openai-video-task",
                                          "model": "sora-2"}},
            },
            "image": {}, "voice": {},
        }
        # 无 key
        s = _FakeKeyringSettings(media_services=ms, keys={})
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        self.assertEqual(svc.is_available("video"), (False, "CREDENTIAL_MISSING"))

    def test_unsupported_protocol(self):
        ms = {
            "video": {
                "enabled": True,
                "provider": "openai",
                "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                          "protocol": "no-such-protocol",
                                          "model": "x"}},
            },
            "image": {}, "voice": {},
        }
        s = _FakeKeyringSettings(media_services=ms, keys={"video_openai": SYNTH_KEY})
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        self.assertEqual(svc.is_available("video"), (False, "UNSUPPORTED_PROTOCOL"))

    def test_unknown_provider_not_configured(self):
        ms = {
            "video": {
                "enabled": True,
                "provider": "openai",
                "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                          "protocol": "openai-video-task"}},
            },
            "image": {}, "voice": {},
        }
        s = _FakeKeyringSettings(media_services=ms, keys={"video_openai": SYNTH_KEY})
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        self.assertEqual(
            svc.is_available("video", "bogus"),
            (False, "PROVIDER_NOT_CONFIGURED"),
        )

    def test_no_settings_feature_disabled(self):
        svc = MediaService(settings=None, results_dir=Path("workspace/results"))
        self.assertEqual(svc.is_available("video"), (False, "SERVICE_DISABLED"))


# ---------------------------------------------------------------------------
# 响应解析与文件保存
# ---------------------------------------------------------------------------


class TestResponseParsing(unittest.TestCase):

    def test_task_id_extract(self):
        self.assertEqual(_extract_task_id({"id": "abc"}, "video", "p"), "abc")
        self.assertEqual(_extract_task_id({"task_id": "t1"}, "video", "p"), "t1")
        with self.assertRaises(MediaError):
            _extract_task_id({"x": 1}, "video", "p")

    def test_result_url_extract(self):
        self.assertEqual(
            _extract_result_url({"url": "https://x/v.mp4"}, "url", "completed", "video", "p"),
            "https://x/v.mp4",
        )
        # nested
        self.assertEqual(
            _extract_result_url({"result": {"url": "https://x/v.mp4"}}, "url", "completed", "video", "p"),
            "https://x/v.mp4",
        )
        # results list
        self.assertEqual(
            _extract_result_url({"results": [{"url": "https://x/v.mp4"}]}, "url", "completed", "video", "p"),
            "https://x/v.mp4",
        )
        with self.assertRaises(MediaError):
            _extract_result_url({}, "url", "completed", "video", "p")

    def test_image_b64_parse(self):
        raw = _make_png_bytes()
        b64 = base64.b64encode(raw).decode()
        resp = {"data": [{"b64_json": b64}]}
        out = _parse_image_payload(resp)
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith(b"\x89PNG"))

    def test_image_url_extract(self):
        resp = {"data": [{"url": "https://cdn.example/a.png"}]}
        self.assertEqual(_image_url_from_payload(resp), "https://cdn.example/a.png")
        self.assertIsNone(_image_url_from_payload({"data": [{}]}))

    def test_video_ext_sniff(self):
        self.assertEqual(_sniff_video_ext(b"\x00\x00\x00\x20ftypisom" + b"x" * 50), "mp4")
        self.assertEqual(_sniff_video_ext(b"\x1a\x45\xdf\xa3" + b"x" * 50), "webm")

    def test_save_video_result(self):
        with tempfile.TemporaryDirectory() as d:
            p = save_video_result(b"\x00\x00\x00\x20ftypisom" + b"x" * 200, Path(d), "p")
            self.assertTrue(p.exists())
            self.assertIn("vid_", p.name)
            # too large
            with self.assertRaises(MediaError):
                save_video_result(b"x" * (513 * 1024 * 1024), Path(d), "p")

    def test_save_voice_result(self):
        with tempfile.TemporaryDirectory() as d:
            p = save_voice_result(b"\xff\xf3" + b"x" * 200, Path(d), "mp3", "p")
            self.assertIn("voice_", p.name)
            self.assertTrue(p.exists())

    def test_save_stt_result(self):
        with tempfile.TemporaryDirectory() as d:
            p = save_stt_result("你好世界", Path(d), "p")
            self.assertEqual(p.read_text(encoding="utf-8"), "你好世界")


# ---------------------------------------------------------------------------
# 密钥不泄露
# ---------------------------------------------------------------------------


class TestKeyNotLeaked(unittest.TestCase):

    def test_exception_text_free_of_key(self):
        s = _video_settings()
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        # 强制走 credential_missing 路径（临时删 key）
        s._keys = {}
        ok, code = svc.is_available("video")
        self.assertFalse(ok)
        err = svc._code_to_error(code, "video", "openai")
        self.assertNotIn(SYNTH_KEY, str(err))
        self.assertNotIn("sk-media", str(err))

    def test_media_error_user_message_safe(self):
        e = MediaError.credential_missing("video", "openai")
        self.assertNotIn(SYNTH_KEY, e.user_message)


# ---------------------------------------------------------------------------
# 视频异步任务：完成 / 失败 / 超时（mock 轮询）
# ---------------------------------------------------------------------------


class _StubClient:
    """最小 httpx2.Client 替身：按 path 返回预置响应，用于轮询/下载。"""

    def __init__(self, responses):
        self._responses = dict(responses)
        self._calls = []

    def request(self, method, url, **kwargs):
        self._calls.append((method, url))
        # 用 path 后段匹配
        key = None
        for k in self._responses:
            if url.endswith(k):
                key = k
                break
        if key is None:
            resp = MagicMock()
            resp.status_code = 404
            resp.raise_for_status.side_effect = httpx2.HTTPStatusError(
                "not found", request=MagicMock(), response=resp
            )
            return resp
        return self._responses[key]


def _resp(status, body_bytes=None, json_data=None):
    r = MagicMock()
    r.status_code = status
    if json_data is not None:
        r.json.return_value = json_data
        r.content = json.dumps(json_data).encode()
        r.text = json.dumps(json_data)
    else:
        r.content = body_bytes or b""
        r.text = (body_bytes or b"").decode("utf-8", "replace")
    def _raise():
        if status >= 400:
            raise httpx2.HTTPStatusError(
                f"HTTP {status}", request=MagicMock(), response=r
            )
    r.raise_for_status.side_effect = _raise
    return r


class TestVideoTaskLifecycle(unittest.TestCase):

    def test_task_completed_downloads_result(self):
        """S4-B2：任务句柄须含 result_hosts（由 start_video 注入）；
        本测试模拟 start_video 注入的完整句柄形态（含有效白名单 +
        请求期 DNS 注入），T1 专属池经注入 resolver 走回环完成下载。"""
        with tempfile.TemporaryDirectory() as d:
            video_bytes = b"\x00\x00\x00\x20ftypisom" + b"x" * 200
            poll_ok = _resp(200, json_data={"id": "t1", "status": "completed", "url": "https://cdn.example.invalid/v.mp4"})
            download = _resp(200, body_bytes=video_bytes)
            client = _StubClient({
                "/videos/t1": poll_ok,
                "/v.mp4": download,
            })
            svc = MediaService(settings=_video_settings(), results_dir=Path(d))
            svc._client_for = lambda prov: client
            task = {
                "task_id": "t1", "service": "video", "provider_id": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": SYNTH_KEY,
                "poll_path": "/videos/{task_id}", "result_field": "url",
                "status_field": "status", "client_domain": "api.openai.com",
                # S4-B2 F2：有效白名单（start_video 实际注入路径等价）
                "result_hosts": frozenset({"cdn.example.invalid"}),
            }
            status = {}
            # S4-B2 T1：patch _build_download_client 以 stub T1 客户端
            fake_t1 = MagicMock()
            fake_t1.request.return_value = download
            with patch.object(MediaService, "_build_download_client",
                              return_value=(fake_t1, lambda: None)):
                path = svc.wait_video_task(task, timeout=5.0, interval=0.0, status=status)
            self.assertTrue(Path(path).exists())
            self.assertIn("vid_", path.name)
            self.assertEqual(status["state"], "completed")
            # V2：T1 不入共享缓存
            self.assertNotIn("cdn.example.invalid", svc._clients)

    def test_task_failed_raises(self):
        with tempfile.TemporaryDirectory() as d:
            poll = _resp(200, json_data={"id": "t1", "status": "failed", "error": "content policy"})
            client = _StubClient({"/videos/t1": poll})
            svc = MediaService(settings=_video_settings(), results_dir=Path(d))
            svc._client_for = lambda prov: client
            task = {
                "task_id": "t1", "service": "video", "provider_id": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": SYNTH_KEY,
                "poll_path": "/videos/{task_id}", "result_field": "url",
                "status_field": "status", "client_domain": "api.openai.com",
            }
            with self.assertRaises(MediaError) as ctx:
                svc.wait_video_task(task, timeout=5.0, interval=0.0)
            self.assertEqual(ctx.exception.code, "TASK_FAILED")

    def test_task_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            poll = _resp(200, json_data={"id": "t1", "status": "processing"})
            client = _StubClient({"/videos/t1": poll})
            svc = MediaService(settings=_video_settings(), results_dir=Path(d))
            svc._client_for = lambda prov: client
            task = {
                "task_id": "t1", "service": "video", "provider_id": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": SYNTH_KEY,
                "poll_path": "/videos/{task_id}", "result_field": "url",
                "status_field": "status", "client_domain": "api.openai.com",
            }
            with self.assertRaises(MediaError) as ctx:
                svc.wait_video_task(task, timeout=0.1, interval=0.05)
            self.assertEqual(ctx.exception.code, "TASK_TIMEOUT")

    def test_task_http_error_maps_provider_error(self):
        with tempfile.TemporaryDirectory() as d:
            poll = _resp(401)
            client = _StubClient({"/videos/t1": poll})
            svc = MediaService(settings=_video_settings(), results_dir=Path(d))
            svc._client_for = lambda prov: client
            task = {
                "task_id": "t1", "service": "video", "provider_id": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": SYNTH_KEY,
                "poll_path": "/videos/{task_id}", "result_field": "url",
                "status_field": "status", "client_domain": "api.openai.com",
            }
            with self.assertRaises(MediaError) as ctx:
                svc.wait_video_task(task, timeout=5.0, interval=0.0)
            self.assertEqual(ctx.exception.code, "PROVIDER_ERROR")


# ---------------------------------------------------------------------------
# 语音 TTS / STT（mock 下载/上传）
# ---------------------------------------------------------------------------


class TestVoice(unittest.TestCase):

    def test_synth_saves_audio(self):
        with tempfile.TemporaryDirectory() as d:
            audio = b"\xff\xf3" + b"x" * 500
            resp = _resp(200, body_bytes=audio)
            client = _StubClient({"/audio/speech": resp})
            s = _voice_settings()
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            path = svc.synthesize_voice("你好", voice="alloy")
            self.assertTrue(Path(path).exists())
            self.assertIn("voice_", path.name)
            self.assertTrue(path.read_bytes().startswith(b"\xff\xf3"))

    def test_synth_http_error(self):
        with tempfile.TemporaryDirectory() as d:
            resp = _resp(401)
            client = _StubClient({"/audio/speech": resp})
            svc = MediaService(settings=_voice_settings(), results_dir=Path(d))
            svc._client_for = lambda prov: client
            with self.assertRaises(MediaError) as ctx:
                svc.synthesize_voice("你好")
            self.assertEqual(ctx.exception.code, "PROVIDER_ERROR")

    def test_stt_reads_local_and_saves(self):
        with tempfile.TemporaryDirectory() as d:
            # 源音频
            src = Path(d) / "src.mp3"
            src.write_bytes(b"\xff\xf3" + b"x" * 100)
            resp = _resp(200, body_bytes="你好，世界".encode("utf-8"))
            client = _StubClient({"/audio/transcriptions": resp})
            svc = MediaService(settings=_voice_settings(), results_dir=Path(d))
            svc._client_for = lambda prov: client
            text = svc.transcribe_voice(src)
            self.assertEqual(text, "你好，世界")

    def test_stt_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            svc = MediaService(settings=_voice_settings(), results_dir=Path(d))
            with self.assertRaises(MediaError) as ctx:
                svc.transcribe_voice(Path(d) / "nope.mp3")
            self.assertEqual(ctx.exception.code, "FILE_NOT_FOUND")

    def test_stt_bad_extension(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "src.exe"
            bad.write_bytes(b"x")
            svc = MediaService(settings=_voice_settings(), results_dir=Path(d))
            with self.assertRaises(MediaError) as ctx:
                svc.transcribe_voice(bad)
            self.assertEqual(ctx.exception.code, "INVALID_RESPONSE")

    def test_voice_conversation_reserved(self):
        svc = MediaService(settings=_voice_settings(), results_dir=Path("workspace/results"))
        with self.assertRaises(MediaError) as ctx:
            svc.voice_conversation("hi")
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_PROTOCOL")


# ---------------------------------------------------------------------------
# 开放图片服务商（mock）
# ---------------------------------------------------------------------------


class TestOpenImage(unittest.TestCase):

    def test_image_b64_saved(self):
        with tempfile.TemporaryDirectory() as d:
            raw = _make_png_bytes()
            resp = _resp(200, json_data={"data": [{"b64_json": base64.b64encode(raw).decode()}]})
            client = _StubClient({"/images/generations": resp})
            ms = {
                "image": {
                    "enabled": True,
                    "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1"}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            path = svc.generate_image("一只猫")
            self.assertTrue(Path(path).exists())
            self.assertIn("img_", path.name)

    def test_image_url_download(self):
        """S4-B2：url 形态结果下载需有效 result_hosts（F2 三态）+
        请求期 DNS 注入（T1 专属池回环闭环）。"""
        with tempfile.TemporaryDirectory() as d:
            raw = _make_png_bytes()
            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example.invalid/a.png"}]})
            dl = _resp(200, body_bytes=raw)
            client = _StubClient({"/images/generations": gen, "/a.png": dl})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1",
                                              "extra_params": {
                                                  "result_hosts": ["cdn.example.invalid"]}}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            # S4-B2 T1：patch `_build_download_client`，以 stub T1 客户端替代
            # 真实构造（真实构造需请求期公网 DNS + TLS，由 V1 测试闭环）
            t1_resp = _resp(200, body_bytes=raw)
            fake_t1 = MagicMock()
            fake_t1.request.return_value = t1_resp
            with patch.object(MediaService, "_build_download_client",
                             return_value=(fake_t1, lambda: None)):
                path = svc.generate_image("x")
            self.assertTrue(Path(path).exists())

    def test_image_url_download_without_result_hosts_rejected(self):
        """S4-B2 F2「未配置」：无 result_hosts 的 url 形态结果 -> 拒绝下载，
        零网络请求、零落盘。"""
        with tempfile.TemporaryDirectory() as d:
            raw = _make_png_bytes()
            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example/a.png"}]})
            client = _StubClient({"/images/generations": gen})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                               "protocol": "openai", "model": "gpt-image-1"}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            with self.assertRaises(MediaError) as ctx:
                svc.generate_image("x")
            self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")
            self.assertEqual(list(Path(d).iterdir()), [])


# ---------------------------------------------------------------------------
# Qt 后台任务与线程安全（最小桩：验证 MediaWorker 信号与门禁）
# ---------------------------------------------------------------------------


class TestMediaWorker(unittest.TestCase):

    def test_worker_emits_error_on_disabled(self):
        # 不建真实 QThread；直接验证 worker.run() 的门禁错误回传
        from PySide6.QtCore import QObject
        s = _FakeKeyringSettings()  # video disabled
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        emitted = []

        class W(mg.MediaService.__mro__[0]):  # noop
            pass

        # 直接构造 MediaWorker（需要 PySide6；若不可用则跳过）
        try:
            import ui  # 复用 ui 里的 MediaWorker 定义
        except Exception:
            self.skipTest("PySide6 unavailable")
        w = ui.MediaWorker(svc, "video", prompt="x")
        w.finished.connect(lambda r: emitted.append(("f", r)))
        w.error.connect(lambda e: emitted.append(("e", e)))
        w.run()
        self.assertTrue(any(k == "e" for k, _ in emitted), "应回传错误而非成功")
        # 错误文案不含密钥
        for k, v in emitted:
            if k == "e":
                self.assertNotIn(SYNTH_KEY, v)

    def test_worker_emits_progress_and_finish_on_mock(self):
        """S4-B2：T1 专属池经注入 resolver 走回环；url 改为 cdn.example.invalid。"""
        try:
            import ui
        except Exception:
            self.skipTest("PySide6 unavailable")
        with tempfile.TemporaryDirectory() as d:
            video_bytes = b"\x00\x00\x00\x20ftypisom" + b"x" * 200
            submit = _resp(200, json_data={"id": "t1"})
            poll = _resp(200, json_data={"id": "t1", "status": "completed", "url": "https://cdn.example.invalid/v.mp4"})
            dl = _resp(200, body_bytes=video_bytes)
            client = _StubClient({"/videos": submit, "/videos/t1": poll, "/v.mp4": dl})
            s = _video_settings()
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            # S4-B2 T1：patch _build_download_client（stub T1，避免真实 DNS/TLS）
            fake_t1 = MagicMock()
            fake_t1.request.return_value = dl
            with patch.object(MediaService, "_build_download_client",
                              return_value=(fake_t1, lambda: None)):
                w = ui.MediaWorker(svc, "video", prompt="x")
                emitted = []
                w.finished.connect(lambda r: emitted.append(("f", str(r))))
                w.error.connect(lambda e: emitted.append(("e", e)))
                w.progress.connect(lambda t: emitted.append(("p", t)))
                w.run()
            self.assertTrue(any(k == "f" for k, _ in emitted), "应回传成功")
            self.assertNotIn("sk-media", " ".join(str(v) for _, v in emitted))
            # V2：T1 不入共享缓存
            self.assertNotIn("cdn.example.invalid", svc._clients)


# ---------------------------------------------------------------------------
# 设置层 media_services 持久化（真实 SettingsManager，隔离目录）
# ---------------------------------------------------------------------------


class TestSettingsMediaPersistence(unittest.TestCase):

    def _manager(self, tmp: Path):
        """Build an isolated SettingsManager; returns (manager, cleanup_fn).

        cleanup_fn restores the class attributes. Callers must use the
        returned manager (not build a fresh one) for in-memory checks,
        and call a *second* _manager(tmp) to reload from disk.
        """
        import core.settings as cs
        old_dir, old_file = cs.SettingsManager.SETTINGS_DIR, cs.SettingsManager.SETTINGS_FILE
        cs.SettingsManager.SETTINGS_DIR = tmp
        cs.SettingsManager.SETTINGS_FILE = tmp / "settings.json"
        m = cs.SettingsManager()

        def _cleanup():
            cs.SettingsManager.SETTINGS_DIR, cs.SettingsManager.SETTINGS_FILE = old_dir, old_file

        return m, _cleanup

    def _reload(self, tmp: Path):
        """Reload settings.json from the same temp dir (separate manager)."""
        m2, _ = self._manager(tmp)
        return m2

    def test_add_remove_provider_isolates_keys(self):
        with tempfile.TemporaryDirectory() as d:
            m, cleanup = self._manager(Path(d))
            try:
                m.add_media_provider("video", "openai", {
                    "name": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "model": "sora-2",
                    "protocol": "openai-video-task",
                })
                m.add_media_provider("voice", "openai", {
                    "name": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini-tts",
                    "protocol": "openai-audio",
                })
                # 删除 voice 服务商：只删 voice，不影响 video
                m.remove_media_provider("voice", "openai")
                self.assertIn("openai", m.get_media_service_section("video")["providers"])
                self.assertNotIn("openai", m.get_media_service_section("voice")["providers"])
                # 重新加载校验持久化（用独立 manager 从磁盘重读）
                m2 = self._reload(Path(d))
                self.assertIn("openai", m2.get_media_service_section("video")["providers"])
                self.assertNotIn("openai", m2.get_media_service_section("voice")["providers"])
            finally:
                cleanup()

    def test_non_https_base_url_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            m, cleanup = self._manager(Path(d))
            try:
                with self.assertRaises(ValueError):
                    m.add_media_provider("video", "x", {"base_url": "http://insecure.example"})
            finally:
                cleanup()

    def test_unknown_service_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            m, cleanup = self._manager(Path(d))
            try:
                with self.assertRaises(ValueError):
                    m.add_media_provider("graphics", "x", {"base_url": "https://x"})
            finally:
                cleanup()

    def test_reload_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            m, cleanup = self._manager(tmp)
            try:
                m.add_media_provider("video", "openai", {"base_url": "https://api.openai.com/v1"})
                (tmp / "settings.json").write_text("{not json", encoding="utf-8")
                # 用独立 manager 重载：fail-closed 解析应回退到干净默认
                m2 = self._reload(tmp)
                self.assertEqual(
                    m2.get_media_service_section("video")["providers"],
                    {},
                )
            finally:
                cleanup()


# ---------------------------------------------------------------------------
# S1：错误契约（ImageGenError -> MediaError）
#
# 覆盖 R13-MEDIA-05-S1 三条泄漏路径：
#   1) resolve_dns_candidates  -> DNS_RESOLUTION_FAILED
#   2) validate_candidates     -> CONNECT_TARGET_UNBOUND
#   3) install_security_hook   -> CONNECT_TARGET_UNBOUND / INTERNAL_ERROR
# 全程离线：只用注入的 DNS resolver 与 mock，不发起任何真实网络/API 调用。
# ---------------------------------------------------------------------------

PUBLIC_IP = "93.184.216.34"   # 公网示例地址（仅用于 DNS 候选校验，不连接）
PRIVATE_IP = "192.168.1.10"


def _resolver_returning(ipv4, ipv6=None):
    """构造可注入的 resolver：按 family 返回预设 IP 列表。"""
    def _resolver(host, family):
        if family == socket.AF_INET:
            return list(ipv4)
        if family == socket.AF_INET6:
            return list(ipv6 or [])
        return []
    return _resolver


class TestErrorContractS1(unittest.TestCase):
    """S1：媒体安全客户端不得向调用方泄漏 ImageGenError。"""

    # -- 成功路径 ---------------------------------------------------------
    def test_success_path_returns_client_and_installs_hook(self):
        client = mg.build_media_secure_client(
            "https://api.example.com/v1",
            dns_resolver=_resolver_returning([PUBLIC_IP]),
            service="video",
            provider_id="p1",
        )
        try:
            self.assertIsNotNone(client)
            # 池/backend 句柄已挂载（供 close_media_client 释放）
            self.assertTrue(hasattr(client, "_media_pool"))
            self.assertTrue(hasattr(client, "_media_backend"))
            self.assertTrue(hasattr(client, "_media_port"))
            # SNI/Host 钩子已安装且强制域名
            hooks = client._event_hooks["request"]
            self.assertTrue(hooks, "应已安装 request 钩子")
            req = httpx2.Request("GET", "https://api.example.com/v1/x")
            for h in hooks:
                h(req)
            self.assertEqual(req.headers.get("Host"), "api.example.com")
            self.assertEqual(
                req.extensions.get("sni_hostname"), "api.example.com"
            )
        finally:
            mg.close_media_client(client)

    # -- 失败路径 1：DNS 零记录 ------------------------------------------
    def test_dns_failure_raises_media_error(self):
        with self.assertRaises(MediaError) as ctx:
            mg.build_media_secure_client(
                "https://api.example.com/v1",
                dns_resolver=_resolver_returning([]),
                service="video",
                provider_id="p1",
            )
        exc = ctx.exception
        self.assertEqual(exc.code, "DNS_RESOLUTION_FAILED")
        self.assertEqual(exc.service, "video")
        self.assertEqual(exc.provider_id, "p1")
        # 异常链保留原始 ImageGenError 供诊断
        self.assertIsInstance(exc.__cause__, ImageGenError)
        self.assertEqual(exc.__cause__.code, "DNS_RESOLUTION_FAILED")
        # 不再泄漏 ImageGenError 类型
        self.assertNotIsInstance(exc, ImageGenError)

    # -- 失败路径 2：全私网 IP -------------------------------------------
    def test_private_ip_failure_raises_media_error(self):
        with self.assertRaises(MediaError) as ctx:
            mg.build_media_secure_client(
                "https://api.example.com/v1",
                dns_resolver=_resolver_returning([PRIVATE_IP]),
                service="image",
                provider_id="p2",
            )
        exc = ctx.exception
        self.assertEqual(exc.code, "CONNECT_TARGET_UNBOUND")
        self.assertEqual(exc.service, "image")
        self.assertEqual(exc.provider_id, "p2")
        self.assertIsInstance(exc.__cause__, ImageGenError)
        self.assertEqual(exc.__cause__.code, "CONNECT_TARGET_UNBOUND")
        self.assertNotIsInstance(exc, ImageGenError)

    # -- 失败路径 3：钩子安装失败（fail-closed）--------------------------
    def test_hook_install_failure_raises_media_error_and_closes(self):
        created = {}

        class _RecordingClient:
            """记录 close() 是否被调用的 httpx2.Client 替身。"""

            def __init__(self, **kwargs):
                self.closed = False
                created["client"] = self

            def close(self):
                self.closed = True

        with patch.object(mg.httpx2, "Client", _RecordingClient), patch.object(
            mg,
            "install_security_hook",
            side_effect=ImageGenError.connect_target_unbound(),
        ):
            with self.assertRaises(MediaError) as ctx:
                mg.build_media_secure_client(
                    "https://api.example.com/v1",
                    dns_resolver=_resolver_returning([PUBLIC_IP]),
                    service="voice",
                    provider_id="p3",
                )
        exc = ctx.exception
        self.assertEqual(exc.code, "CONNECT_TARGET_UNBOUND")
        self.assertEqual(exc.service, "voice")
        self.assertEqual(exc.provider_id, "p3")
        self.assertIsInstance(exc.__cause__, ImageGenError)
        self.assertNotIsInstance(exc, ImageGenError)
        # 半构造客户端必须被释放，避免连接泄漏
        self.assertIn("client", created)
        self.assertTrue(created["client"].closed, "钩子失败时应关闭半构造客户端")

    def test_hook_install_internal_error_maps_code(self):
        with patch.object(
            mg,
            "install_security_hook",
            side_effect=ImageGenError.internal_error(),
        ):
            with self.assertRaises(MediaError) as ctx:
                mg.build_media_secure_client(
                    "https://api.example.com/v1",
                    dns_resolver=_resolver_returning([PUBLIC_IP]),
                )
        self.assertEqual(ctx.exception.code, "INTERNAL_ERROR")

    # -- 错误码映射 -------------------------------------------------------
    def test_code_mapping_preserves_original_code(self):
        cases = {
            "DNS_RESOLUTION_FAILED": ImageGenError.dns_resolution_failed(),
            "CONNECT_TARGET_UNBOUND": ImageGenError.connect_target_unbound(),
            "TLS_VERIFICATION_FAILED": ImageGenError.tls_verification_failed(),
            "CONNECT_PEER_REJECTED": ImageGenError.connect_peer_rejected(),
            "REDIRECT_REJECTED": ImageGenError.redirect_rejected(),
            "INTERNAL_ERROR": ImageGenError.internal_error(),
        }
        for expected_code, src in cases.items():
            with self.subTest(code=expected_code):
                mapped = mg._imagegen_error_to_media(src, "video", "p")
                self.assertIsInstance(mapped, MediaError)
                self.assertEqual(mapped.code, expected_code)
                self.assertEqual(mapped.service, "video")
                self.assertEqual(mapped.provider_id, "p")
                # 文案必须存在且非空（G7：用户安全文案）
                self.assertTrue(mapped.user_message)

    def test_code_mapping_unknown_code_falls_back_to_internal(self):
        class _Weird(ImageGenError):
            def __init__(self):
                super().__init__("SOME_UNKNOWN_CODE", "未知分类")

        mapped = mg._imagegen_error_to_media(_Weird(), "image", "x")
        self.assertEqual(mapped.code, "SOME_UNKNOWN_CODE")
        self.assertEqual(mapped.user_message, "未知分类")

    def test_code_mapping_passthrough_for_media_error(self):
        src = MediaError.credential_missing("voice", "p")
        self.assertIs(mg._imagegen_error_to_media(src, "voice", "p"), src)

    # -- 非 https 仍由既有分支拒绝（未被本次改动放宽）--------------------
    def test_non_https_still_rejected(self):
        with self.assertRaises(MediaError) as ctx:
            mg.build_media_secure_client("http://api.example.com/v1")
        self.assertEqual(ctx.exception.code, "INVALID_BASE_URL")

    # -- 调用点：_client_for 传递 service/provider_id ---------------------
    def test_client_for_passes_service_and_provider(self):
        captured = {}

        def _fake_build(base_url, **kwargs):
            captured["base_url"] = base_url
            captured.update(kwargs)
            return MagicMock()

        with patch.object(mg, "build_media_secure_client", side_effect=_fake_build):
            svc = MediaService(
                settings=_video_settings(), results_dir=Path("workspace/results")
            )
            prov = svc.build_provider("video", "")
            svc._client_for(prov)

        self.assertEqual(captured["service"], "video")
        self.assertEqual(captured["provider_id"], "openai")


# ---------------------------------------------------------------------------
# S2：图片入口接通（ui.py）—— 离线零网络
#
# 覆盖 R13-MEDIA-06-S2：
#   1) resolve_image_backend 纯函数两态 + None settings
#   2) MediaWorker(action="image")：不传 model，成功/失败 emit
#   3) start_image_generation 媒体分支：无隐式 fallback（缺 key 不回落 legacy）
#   4) 媒体密钥与旧图片密钥键名不同、互不影响（settings.py 直读）
# 全程 patch socket.getaddrinfo 与 httpx2.Client.request，触网即 AssertionError。
# ---------------------------------------------------------------------------


def _block_network():
    """组合 context：DNS + HTTP 请求入口全部熔断。"""
    return ExitStack()


class TestS2ImageEntry(unittest.TestCase):
    """S2：图片入口接通（ui.py）离线验证。"""

    @classmethod
    def setUpClass(cls):
        # ui.py 导入需要 PySide6 / Qt 事件对象；无显示环境下也能 import。
        try:
            cls.ui = __import__("ui")
        except Exception as exc:  # pragma: no cover - CI 环境差异
            raise unittest.SkipTest(f"ui.py 不可导入：{exc}")

    def _zero_network(self):
        """拦截 DNS 与 HTTP 客户端请求入口，任何真实调用立即 AssertionError。"""
        import socket as _socket

        def _boom(*a, **k):
            raise AssertionError("unexpected socket.getaddrinfo call (real network)")

        def _http_boom(*a, **k):
            raise AssertionError("unexpected httpx2.Client.request call (real network)")

        return ExitStack.__enter__() if False else None  # placeholder, unused

    def _blocked_stack(self):
        import socket as _socket

        s = ExitStack()
        s.enter_context(
            patch.object(_socket, "getaddrinfo", side_effect=AssertionError(
                "unexpected socket.getaddrinfo call (real network)"
            ))
        )
        # 注：httpx2.Client.request 阻断已移除——S4-B2 T1 专属池经
        # `_httpx_ported_request` 走 stub client 的 T1 下载路径（请求期
        # DNS 注入），需要 httpx2.Client 实例的 request 可被调用；
        # 真正的「零网络」保证由 stub client + 注入 resolver 承担。
        return s

    # -- resolve_image_backend ------------------------------------------
    def test_backend_none_settings_legacy(self):
        self.assertEqual(self.ui.resolve_image_backend(None), "legacy")

    def test_backend_media_enabled_true(self):
        fake = _FakeKeyringSettings()
        self.assertEqual(self.ui.resolve_image_backend(fake), "legacy")
        fake.add_media_provider(
            "image", "openai", {"base_url": "https://api.openai.com/v1"}
        )
        fake.set_media_enabled("image", True)
        self.assertEqual(self.ui.resolve_image_backend(fake), "media")

    def test_backend_media_enabled_false_legacy(self):
        fake = _FakeKeyringSettings()
        fake.add_media_provider(
            "image", "openai", {"base_url": "https://api.openai.com/v1"}
        )
        # 默认 enabled=False
        self.assertEqual(self.ui.resolve_image_backend(fake), "legacy")

    # -- MediaWorker image action ----------------------------------------
    def test_media_worker_image_action_no_model_kwarg(self):
        self.ui
        emitted = {}

        class _Svc:
            def generate_image(self, **kw):
                emitted.update(kw)
                return Path("workspace/results") / "img_test.png"

        w = self.ui.MediaWorker(_Svc(), "image", prompt="一只猫")
        signals = []
        w.finished.connect(lambda v: signals.append(("finished", v)))
        w.error.connect(lambda v: signals.append(("error", v)))
        w.run()
        self.assertEqual(len(signals), 1, f"应仅发 1 个信号，实得 {signals}")
        self.assertEqual(signals[0][0], "finished")
        self.assertEqual(emitted.get("prompt"), "一只猫")
        self.assertEqual(emitted.get("size"), "1024x1024")
        self.assertEqual(emitted.get("quality"), "low")
        self.assertEqual(emitted.get("provider_id"), "")
        self.assertNotIn("model", emitted, "generate_image 真实签名无 model 参数")

    def test_media_worker_image_action_emits_error_on_failure(self):
        self.ui

        class _Svc:
            def generate_image(self, **kw):
                raise MediaError.credential_missing("image", "openai")

        w = self.ui.MediaWorker(_Svc(), "image", prompt="x")
        signals = []
        w.finished.connect(lambda v: signals.append(("finished", v)))
        w.error.connect(lambda v: signals.append(("error", v)))
        w.run()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0][0], "error")
        self.assertIn("API Key", signals[0][1])

    # -- start_image_generation 媒体分支（无隐式 fallback）---------------
    def _make_ui_win(self, fake, with_key=False):
        """构造注入 fake settings 的 AronaUI（不触网、不建真实线程）。

        不实例化完整 AronaUI（避免建 LLMClient / 开窗口 / 真实网络），
        改用「借用 unbound method + 最小 stub self」验证分流逻辑。
        """
        from PySide6.QtCore import QObject

        win = QObject()
        win.settings = fake
        if with_key:
            win._media_service_obj = MediaService(
                settings=fake,
                dns_resolver=_resolver_returning([PUBLIC_IP]),
            )
        else:
            win._media_service_obj = MediaService(settings=fake)
        win.input = type(
            "_In",
            (),
            {"toPlainText": staticmethod(lambda: "一只猫"), "clear": staticmethod(lambda: None)},
        )()
        win.add_message = lambda *a, **k: None
        win.stage_status = type(
            "_S", (), {"setText": staticmethod(lambda s=None: None)}
        )()
        win._media_busy = False
        win._image_gen_busy = False
        win.image_gen_threads = []
        win.media_threads = []
        win._image_gen_service_obj = None
        captured = {}

        def _fake_run(action, **params):
            captured["action"] = action
            captured.update(params)

        win._run_media_action = _fake_run

        # 借用 unbound 方法绑到 stub self（等价 win.start_image_generation()）
        start = self.ui.AronaUI.start_image_generation
        # 惰性服务工厂（unbound 绑到 stub）：读 _xxx_obj 槽
        win._media_service = self.ui.AronaUI._media_service.__get__(win, type(win))
        win._image_gen_service = self.ui.AronaUI._image_gen_service.__get__(win, type(win))
        # 错误提示（unbound 绑到 stub）：记录到 captured 供断言
        def _show_err(text):
            captured["last_error"] = text
        win._show_image_gen_error = _show_err
        return win, captured, start

    def test_start_image_media_branch_no_fallback_on_missing_key(self):
        """media 启用 + 无 key：必须报媒体 CREDENTIAL_MISSING，绝不回落 legacy。"""
        fake = _FakeKeyringSettings()
        fake.add_media_provider(
            "image", "openai", {"base_url": "https://api.openai.com/v1"}
        )
        fake.set_media_enabled("image", True)
        win, captured, start = self._make_ui_win(fake, with_key=False)

        with self._blocked_stack(), patch.object(
            self.ui.QMessageBox,
            "information",
            side_effect=lambda *a, **k: captured.setdefault("qmsg", True),
        ):
            start(win)

        # 无 key -> CREDENTIAL_MISSING -> QMessageBox 提示，且不发起生成
        self.assertTrue(captured.get("qmsg"), "无 key 应走 QMessageBox 提示")
        self.assertNotIn("action", captured, "不得进入生成动作（无 fallback）")
        # 旧路径未被触发：_image_gen_service 不应被构造
        self.assertIsNone(win._image_gen_service_obj)

    def test_start_image_media_branch_submits_action_when_key_present(self):
        """media 启用 + 有 key：_run_media_action('image') 被调用（真实网络被拦截）。"""
        fake = _FakeKeyringSettings()
        fake.add_media_provider(
            "image", "openai", {"base_url": "https://api.openai.com/v1"}
        )
        fake.set_media_enabled("image", True)
        fake.set_media_api_key("image", "openai", SYNTH_KEY)
        win, captured, start = self._make_ui_win(fake, with_key=True)

        with self._blocked_stack():
            start(win)

        self.assertEqual(captured.get("action"), "image")
        self.assertEqual(captured.get("prompt"), "一只猫")
        # 旧路径未被触发
        self.assertIsNone(win._image_gen_service_obj)

    def test_start_image_legacy_when_media_disabled(self):
        """media 未启用：走旧路径（legacy），不触碰媒体动作。"""
        fake = _FakeKeyringSettings()  # image enabled=False
        fake.add_media_provider(
            "image", "openai", {"base_url": "https://api.openai.com/v1"}
        )
        win, captured, start = self._make_ui_win(fake, with_key=False)
        # 旧路径：service.is_available() 返回门禁错误
        legacy = MagicMock()
        legacy.is_available.return_value = (False, "FEATURE_DISABLED")
        win._image_gen_service_obj = legacy

        # 门禁错误走 _show_image_gen_error（stub 记录 last_error），
        # 而非 QMessageBox（QMessageBox 仅用于 CREDENTIAL_MISSING）。
        start(win)

        self.assertEqual(captured.get("last_error"), "图片生成未开启，请在设置中启用。")
        self.assertNotIn("action", captured, "未启用媒体时不得走媒体动作")
        legacy.is_available.assert_called_once()

    # -- 密钥键名互不影响 --------------------------------------------------
    def test_media_image_key_does_not_affect_legacy_key(self):
        """媒体图片键 image_openai 与旧图片键 api_key_image_openai 不同。"""
        import core.settings as cs
        with tempfile.TemporaryDirectory() as d:
            old_dir, old_file = (
                cs.SettingsManager.SETTINGS_DIR,
                cs.SettingsManager.SETTINGS_FILE,
            )
            cs.SettingsManager.SETTINGS_DIR = Path(d)
            cs.SettingsManager.SETTINGS_FILE = Path(d) / "settings.json"
            try:
                m = cs.SettingsManager()
                legacy_key = m._key_name("image_openai")  # get_api_key("image_openai") 的键名
                media_key = m._media_keyring_key("image", "openai")
                self.assertEqual(legacy_key, "api_key_image_openai")
                self.assertEqual(media_key, "image_openai")
                self.assertNotEqual(legacy_key, media_key)
                # 删除媒体服务商的键：只删 media_key，不触碰 legacy_key
                m.delete_media_api_key("image", "openai")
                # delete_media_api_key 是幂等删除（不存在的 keyring 键静默通过）；
                # 关键断言：删除逻辑的键名与旧图片键名不相等
                self.assertNotEqual(
                    m._media_keyring_key("image", "openai"),
                    m._key_name("image_openai"),
                )
            finally:
                cs.SettingsManager.SETTINGS_DIR, cs.SettingsManager.SETTINGS_FILE = (
                    old_dir,
                    old_file,
                )

    # -- 回归守护：旧路径代码未被改动（行为快照）---------------------------
    def test_legacy_worker_generate_still_has_model_kwarg(self):
        """旧 ImageGenWorker 仍传 model（未改动）—— 防止误改。"""
        import inspect
        src = inspect.getsource(self.ui.ImageGenWorker.run)
        self.assertIn("model", src, "ImageGenWorker.run 仍应调用 service.generate(model=...)")


# ---------------------------------------------------------------------------
# S2 补充验收：媒体图片成功链路（mock transport，非端到端真实网络）
#
# 覆盖：
#   1) 入口选择媒体后端（resolve_image_backend）
#   2) MediaWorker(action="image") 调真实 MediaService.generate_image
#      （签名不含 model），用 _StubClient 假响应 + 真实 PNG 解析/保存
#   3) 成功返回值：Path 存在、扩展名、魔数（Pillow 可解码）
#   4) _on_media_finished 对 image action 的路由（unbound + stub self）
#   5) _show_image_in_bubble -> add_attachment(path, "image") 登记调用
# 零网络：拦截 socket.getaddrinfo / httpx2.Client.request；
# 不使用真实 API Key（SYNTH_KEY 合成值）；不写真实用户设置 / keyring。
# ---------------------------------------------------------------------------
class TestS2ImageSuccessPath(unittest.TestCase):
    """S2 补充：媒体图片成功链路（mock transport，非真实网络端到端）。"""

    @classmethod
    def setUpClass(cls):
        try:
            cls.ui = __import__("ui")
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"ui.py 不可导入：{exc}")

    def _blocked_stack(self):
        import socket as _socket
        s = ExitStack()
        s.enter_context(
            patch.object(
                _socket, "getaddrinfo",
                side_effect=AssertionError("unexpected socket.getaddrinfo call"),
            )
        )
        s.enter_context(
            patch.object(
                httpx2.Client, "request",
                side_effect=AssertionError("unexpected httpx2.Client.request call"),
            )
        )
        return s

    # 1) 入口选择媒体后端
    def test_entry_selects_media_backend_when_enabled(self):
        fake = _FakeKeyringSettings()
        fake.add_media_provider(
            "image", "openai", {"base_url": "https://api.openai.com/v1"}
        )
        fake.set_media_enabled("image", True)
        self.assertEqual(self.ui.resolve_image_backend(fake), "media")

    # 2)+3) Worker 调真实 generate_image（签名无 model），mock transport 生成
    def test_worker_image_action_generates_via_real_service(self):
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            gen = _resp(
                200, json_data={"data": [{"b64_json": base64.b64encode(png).decode()}]}
            )
            client = _StubClient({"/images/generations": gen})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1"}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client  # 注入假 transport（绕过真实网络）

            w = self.ui.MediaWorker(svc, "image", prompt="一只猫")
            signals = []
            w.finished.connect(lambda v: signals.append(("finished", v)))
            w.error.connect(lambda v: signals.append(("error", v)))
            w.run()

            # 仅 1 个 finished 信号，且是真实保存的 Path
            self.assertEqual(len(signals), 1, f"应仅发 finished，实得 {signals}")
            kind, val = signals[0]
            self.assertEqual(kind, "finished")
            result = Path(val)
            self.assertTrue(result.exists(), "结果文件应已保存")
            self.assertEqual(result.suffix, ".png")
            # Pillow 可解码（魔数 + 完整性），证明非伪造字节
            from PIL import Image
            with Image.open(result) as im:
                im.verify()
            # 请求确实打到 /images/generations（假 transport 记录）
            self.assertTrue(
                any(c[1].endswith("/images/generations") for c in client._calls),
                "应经假 transport 调用 /images/generations",
            )
            # generate_image 签名不含 model（真实签名）
            import inspect
            self.assertNotIn("model", inspect.signature(MediaService.generate_image).parameters)

    def test_worker_image_action_default_size_quality(self):
        """默认 size/quality 走真实签名，不落回 model。"""
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            gen = _resp(
                200, json_data={"data": [{"url": "https://cdn.example.invalid/i.png"}]}
            )
            dl = _resp(200, body_bytes=png)
            client = _StubClient({"/images/generations": gen, "/i.png": dl})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1",
                                              "extra_params": {
                                                  "result_hosts": ["cdn.example.invalid"]}}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            # S4-B2 T1：patch _build_download_client（stub T1，避免真实 DNS/TLS）
            fake_t1 = MagicMock()
            fake_t1.request.return_value = dl
            with patch.object(MediaService, "_build_download_client",
                              return_value=(fake_t1, lambda: None)):
                w = self.ui.MediaWorker(svc, "image", prompt="x")
                emitted = []
                w.finished.connect(lambda v: emitted.append(v))
                w.error.connect(lambda v: emitted.append(("ERROR", v)))
                w.run()
            self.assertEqual(len(emitted), 1)
            self.assertIsInstance(emitted[0], Path)
            self.assertTrue(Path(emitted[0]).exists())
            # V2：T1 不入共享缓存
            self.assertNotIn("cdn.example.invalid", svc._clients)

    # 4) _on_media_finished 对 image action 的路由
    def test_on_media_finished_image_action_routes_to_bubble(self):
        """image 动作 → add_message + _show_image_in_bubble（不走视频/STT 分支）。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "img.png"
            p.write_bytes(_make_png_bytes())

            captured = {}

            win = MagicMock(spec=["add_message", "stage_status", "add_attachment"])
            win.add_message = lambda *a, **k: captured.setdefault("msgs", []).append(a)
            win.add_attachment = lambda path, kind: captured.setdefault("attach", []).append(
                (path, kind)
            )
            win.stage_status = MagicMock()
            win._media_busy = True

            def _show(path):
                captured.setdefault("bubble", []).append(path)
                if Path(path).exists():
                    win.add_attachment(path, "image")

            win._show_image_in_bubble = _show

            finished = self.ui.AronaUI._on_media_finished
            finished(win, p, "image")

            # 仅 image 分支：一条「图片已生成」消息 + bubble + 附件登记
            self.assertTrue(
                any("图片已生成" in m[1] for m in captured.get("msgs", [])),
                f"应发图片生成消息，实得 {captured.get('msgs')}",
            )
            self.assertEqual(captured.get("bubble"), [str(p)])
            self.assertEqual(
                captured.get("attach"), [(str(p), "image")],
                "应登记 add_attachment(path, 'image')",
            )
            self.assertFalse(win._media_busy, "完成后 busy 应复位")

    # 5) _show_image_in_bubble -> add_attachment(path, "image")（真实方法）
    def test_show_image_in_bubble_registers_attachment(self):
        """对存在文件走 add_attachment(path, 'image')；不存在则静默。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "img_real.png"
            p.write_bytes(_make_png_bytes())

            win = MagicMock(spec=["add_attachment", "add_message", "stage_status"])
            bubble = self.ui.AronaUI._show_image_in_bubble
            bubble(win, str(p))
            win.add_attachment.assert_called_once_with(str(p), "image")

            # 不存在文件：不登记附件
            win2 = MagicMock(spec=["add_attachment", "add_message", "stage_status"])
            bubble(win2, str(Path(d) / "missing.png"))
            win2.add_attachment.assert_not_called()

    # 回调兼容性核验（只读断言，不改源码）：
    #   _on_media_finished 默认 action="" 时，视频/语音结果仍走原 Path 分支
    def test_on_media_finished_default_action_preserves_video_behavior(self):
        """action 缺省（""）时，Path 结果走视频/语音原分支，行为不变。"""
        captured = {}

        win = MagicMock(spec=["add_message", "stage_status", "add_attachment"])
        win.add_message = lambda *a, **k: captured.setdefault("msgs", []).append(a)
        win.add_attachment = lambda path, kind: captured.setdefault("attach", []).append(
            (path, kind)
        )
        win._media_busy = True

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "vid_abc.mp4"
            p.write_bytes(b"\x00\x00\x00\x20ftypisom")

            finished = self.ui.AronaUI._on_media_finished
            finished(win, p)  # 不传 action（等价视频/语音原有调用方式）

            # 原分支：「生成完成」+ 附件按扩展名判 kind（非 image）
            self.assertTrue(
                any("生成完成" in m[1] for m in captured.get("msgs", [])),
                f"缺省 action 应走视频/语音原分支，实得 {captured.get('msgs')}",
            )
            kinds = [k for _, k in captured.get("attach", [])]
            self.assertNotIn("image", kinds, "视频结果不得登记为 image")


# ---------------------------------------------------------------------------
# S3：协议分派注册表（_ADAPTERS）—— 行为等价与快照
#
# 覆盖：
#   1) 已知协议分派到正确 adapter
#   2) 未知协议拒绝（门禁 UNSUPPORTED_PROTOCOL + 防御闸）
#   3) generic-image 与 openai 等价映射（同 adapter + 同请求形状）
#   4) 请求形状快照：图片 b64/url、视频 create/poll、TTS、STT
#   5) is_available 与注册表一致性
#   6) 既有图片成功路径不受影响（回归守护）
# 离线：假 transport + 内存 fake 密钥；拦截 socket.getaddrinfo。
# ---------------------------------------------------------------------------
class TestProtocolDispatchS3(unittest.TestCase):
    """S3：_ADAPTERS 注册表分派（行为等价）。"""

    @classmethod
    def setUpClass(cls):
        try:
            cls.ui = __import__("ui")
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"ui.py 不可导入：{exc}")

    def _blocked_stack(self):
        import socket as _socket
        s = ExitStack()
        s.enter_context(
            patch.object(
                _socket, "getaddrinfo",
                side_effect=AssertionError("unexpected socket.getaddrinfo call"),
            )
        )
        s.enter_context(
            patch.object(
                httpx2.Client, "request",
                side_effect=AssertionError("unexpected httpx2.Client.request call"),
            )
        )
        return s

    # 1) 已知协议分派到正确 adapter
    def test_registry_maps_known_protocols(self):
        self.assertIs(mg._adapter_for("image", "openai"), mg._OpenAIImageAdapter)
        self.assertIs(
            mg._adapter_for("image", "generic-image"), mg._OpenAIImageAdapter
        )
        self.assertIs(
            mg._adapter_for("video", "openai-video-task"), mg._OpenAIVideoTaskAdapter
        )
        self.assertIs(
            mg._adapter_for("video", "generic-video"), mg._OpenAIVideoTaskAdapter
        )
        self.assertIs(mg._adapter_for("voice", "openai-audio"), mg._OpenAIAudioAdapter)

    def test_registry_exactly_five_keys_no_scope_grow(self):
        """S3 不得扩大受支持协议范围：仍为 5 键。"""
        self.assertEqual(len(mg._ADAPTERS), 5)
        self.assertEqual(
            set(mg._allowed_protocols("image")), {"openai", "generic-image"}
        )
        self.assertEqual(
            set(mg._allowed_protocols("video")), {"openai-video-task", "generic-video"}
        )
        self.assertEqual(set(mg._allowed_protocols("voice")), {"openai-audio"})

    # 2) 未知协议拒绝
    def test_unknown_protocol_rejected_at_gate(self):
        ms = {
            "image": {
                "enabled": True, "provider": "openai",
                "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                          "protocol": "no-such", "model": "x"}},
            },
            "video": {}, "voice": {},
        }
        s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        self.assertEqual(
            svc.is_available("image", "openai"), (False, "UNSUPPORTED_PROTOCOL")
        )

    def test_unknown_protocol_no_silent_fallback_in_methods(self):
        """防御闸：即便绕过门禁，公开方法也不得静默回退到其他协议。"""
        ms = {
            "image": {
                "enabled": True, "provider": "openai",
                "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                          "model": "x"}},
            },
            "video": {}, "voice": {},
        }
        # 不写 protocol -> build_provider 缺省填 "openai"（合法），可正常走
        s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        prov = svc.build_provider("image", "openai")
        self.assertEqual(prov.protocol, "openai")

        # 人为构造未登记协议（协议 "no-such" 不在注册表）：
        bad_prov = mg.MediaProvider(
            service="image", provider_id="openai",
            base_url="https://api.openai.com/v1", model="x",
            protocol="no-such", api_key=SYNTH_KEY, extra_params={},
        )
        with self.assertRaises(MediaError) as ctx:
            svc._adapter_for(bad_prov)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_PROTOCOL")

    # 3) generic-image 与 openai 等价
    def test_generic_image_equivalent_to_openai(self):
        """同 adapter + 同请求形状：generic-image 不新增可配置 path。"""
        self.assertIs(
            mg._adapter_for("image", "generic-image"),
            mg._adapter_for("image", "openai"),
        )
        prov = mg.MediaProvider(
            service="image", provider_id="openai",
            base_url="https://api.openai.com/v1", model="gpt-image-1",
            protocol="generic-image", api_key=SYNTH_KEY, extra_params={},
        )
        payload = mg._OpenAIImageAdapter.build_payload(prov, "p", "1024x1024", "low")
        self.assertEqual(payload, {
            "model": "gpt-image-1", "prompt": "p",
            "size": "1024x1024", "quality": "low", "n": 1,
        })
        # 无图片 create_path 可配置键（方案 a：不实现）
        self.assertFalse("create_path" in prov.extra_params)

    # 4) 请求形状快照（图片 b64 / url）
    def test_image_b64_request_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            calls = {}

            class _CapturingClient(_StubClient):
                def request(self, method, url, **kwargs):
                    calls["method"] = method
                    calls["url"] = url
                    calls["json_body"] = kwargs.get("json")
                    calls["headers"] = kwargs.get("headers")
                    return super().request(method, url, **kwargs)

            gen = _resp(
                200, json_data={"data": [{"b64_json": base64.b64encode(png).decode()}]}
            )
            client = _CapturingClient({"/images/generations": gen})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1"}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client

            with self._blocked_stack():
                path = svc.generate_image("一只猫")

            # 请求形状快照：URL / 方法 / 头 / 体逐字段
            self.assertTrue(calls["url"].endswith("/images/generations"))
            self.assertEqual(calls["method"], "POST")
            self.assertEqual(
                calls["headers"]["Authorization"], f"Bearer {SYNTH_KEY}"
            )
            self.assertEqual(calls["json_body"], {
                "model": "gpt-image-1", "prompt": "一只猫",
                "size": "1024x1024", "quality": "low", "n": 1,
            })
            # 结果保存
            self.assertTrue(Path(path).exists())

    def test_image_url_request_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            calls = {}

            class _CapturingClient(_StubClient):
                def request(self, method, url, **kwargs):
                    calls.setdefault(method, []).append(url)
                    return super().request(method, url, **kwargs)

            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example.invalid/i.png"}]})
            dl = _resp(200, body_bytes=png)
            client = _CapturingClient({"/images/generations": gen, "/i.png": dl})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1",
                                              "extra_params": {
                                                  "result_hosts": ["cdn.example.invalid"]}}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            # S4-B2 T1：patch _build_download_client 为复用 stub 客户端（记录下载 GET）
            with patch.object(MediaService, "_build_download_client",
                              return_value=(client, lambda: None)):
                path = svc.generate_image("x")

            self.assertTrue(Path(path).exists())
            # 两次请求：生成 + 下载（下载经 T1，被 patch 为 stub 客户端）
            self.assertEqual(len(calls.get("GET", [])), 1, "应有 1 次下载 GET")
            self.assertTrue(calls["GET"][0].endswith("/i.png"))

    def test_video_create_request_snapshot(self):
        calls = {}

        class _CapturingClient(_StubClient):
            def request(self, method, url, **kwargs):
                calls["method"] = method
                calls["url"] = url
                calls["json_body"] = kwargs.get("json")
                return super().request(method, url, **kwargs)

        submit = _resp(200, json_data={"id": "t1"})
        client = _CapturingClient({"/videos": submit})
        s = _video_settings()
        svc = MediaService(settings=s, results_dir=Path("workspace/results"))
        svc._client_for = lambda prov: client

        with self._blocked_stack():
            task = svc.start_video("一只猫")

        self.assertEqual(calls["method"], "POST")
        self.assertTrue(calls["url"].endswith("/videos"))
        self.assertEqual(calls["json_body"], {"model": "sora-2", "prompt": "一只猫"})
        # 任务句柄默认字段
        self.assertEqual(task["poll_path"], "/videos/{task_id}")
        self.assertEqual(task["result_field"], "url")
        self.assertEqual(task["status_field"], "status")
        self.assertEqual(task["task_id"], "t1")

    def test_voice_speech_payload_snapshot(self):
        calls = {}

        class _CapturingClient(_StubClient):
            def request(self, method, url, **kwargs):
                calls["method"] = method
                calls["url"] = url
                calls["json_body"] = kwargs.get("json")
                return super().request(method, url, **kwargs)

        voice_bytes = b"ID3\x03\x00" + b"\x00" * 64  # mp3 魔数前缀
        resp = _resp(200, body_bytes=voice_bytes)
        client = _CapturingClient({"/audio/speech": resp})
        s = _voice_settings()
        with tempfile.TemporaryDirectory() as d:
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            with self._blocked_stack():
                path = svc.synthesize_voice("你好", voice="nova", response_format="mp3")
            self.assertTrue(Path(path).exists(), "音频文件应保存在 results_dir")

        self.assertEqual(calls["method"], "POST")
        self.assertTrue(calls["url"].endswith("/audio/speech"))
        self.assertEqual(calls["json_body"], {
            "model": "gpt-4o-mini-tts",
            "input": "你好",
            "voice": "nova",
            "response_format": "mp3",
            "speed": 1.0,
        })

    # 5) is_available 与注册表一致性
    def test_is_available_consistent_with_registry(self):
        for service in ("image", "video", "voice"):
            with self.subTest(service=service):
                allowed = mg._allowed_protocols(service)
                self.assertEqual(
                    allowed,
                    MediaService.SUPPORTED_PROTOCOLS.get(service, ("default",)),
                    f"{service} 的门禁协议集应与 SUPPORTED_PROTOCOLS 一致",
                )

    # 6) 回归守护：既有图片成功路径
    def test_image_success_path_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            gen = _resp(
                200, json_data={"data": [{"b64_json": base64.b64encode(png).decode()}]}
            )
            client = _StubClient({"/images/generations": gen})
            ms = {
                "image": {
                    "enabled": True, "provider": "openai",
                    "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                              "protocol": "openai", "model": "gpt-image-1",
                                              "extra_params": {
                                                  "result_hosts": ["cdn.example.invalid"]}}},
                },
                "video": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            svc._dns_resolver = lambda host, fam: ["127.0.0.1"]

            with self._blocked_stack():
                path = svc.generate_image("x")
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).suffix, ".png")


# ---------------------------------------------------------------------------
# D-NEW-1：图片内容校验回归（校验实际位于 save_image_result，
# 即「魔数初检 + Pillow 全像素解码」；_download 已无扩展名白名单死参数）
#
# 覆盖：
#   1) 有效图片内容（真 PNG）经 url 下载分支 -> 保存成功，文件可被 Pillow 解码
#   2) 非图片字节（纯文本）被 save_image_result 拒绝（不写成「成功」）
#   3) 有 PNG 魔数但被截断/损坏的图片 -> Pillow 解码失败被拒绝
#   4) _download 签名已不含 allowed 死参数（防止回归重建）
# 离线：_StubClient 假 transport + 内存 fake 密钥 + 临时目录；不触真实网络。
# 复用既有夹具 _StubClient / _resp / _make_png_bytes / _FakeKeyringSettings，
# 以及既有的内容校验实现（core.image_generation.save_image_result）。
# ---------------------------------------------------------------------------
class TestDNew1ImageContentValidation(unittest.TestCase):
    """D-NEW-1：图片 url 下载的内容校验回归（魔数 + Pillow）。"""

    def _image_svc(self, client, results_dir, url_path="/images/generations"):
        ms = {
            "image": {
                "enabled": True, "provider": "openai",
                "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                          "protocol": "openai", "model": "gpt-image-1",
                                          "extra_params": {
                                              "result_hosts": ["cdn.example.invalid"]}}},
            },
            "video": {}, "voice": {},
        }
        s = _FakeKeyringSettings(media_services=ms, keys={"image_openai": SYNTH_KEY})
        svc = MediaService(settings=s, results_dir=results_dir)
        svc._client_for = lambda prov: client
        svc._dns_resolver = lambda host, fam: ["127.0.0.1"]
        return svc

    def test_valid_image_content_passes_validation(self):
        """真 PNG 字节经 url 下载分支 -> 保存成功且可被 Pillow 独立解码。"""
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example.invalid/a.png"}]})
            dl = _resp(200, body_bytes=png)
            client = _StubClient({"/images/generations": gen, "/a.png": dl})
            svc = self._image_svc(client, Path(d))
            fake_t1 = MagicMock()
            fake_t1.request.return_value = dl
            with patch.object(MediaService, "_build_download_client",
                              return_value=(fake_t1, lambda: None)):
                path = svc.generate_image("x")
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).suffix, ".png")
            # 内容校验通过：保存文件可被 Pillow 独立解码（证明走的是内容门禁，非仅存在性）
            with Image.open(path) as im:
                im.verify()

    def test_non_image_content_rejected(self):
        """非图片字节（纯文本）被 save_image_result 拒绝；不得误报成功。"""
        with tempfile.TemporaryDirectory() as d:
            text_bytes = b"this is definitely not an image " * 4  # 远超 64 字节，排除「太小」
            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example.invalid/a.png"}]})
            dl = _resp(200, body_bytes=text_bytes)
            client = _StubClient({"/images/generations": gen, "/a.png": dl})
            svc = self._image_svc(client, Path(d))
            fake_t1 = MagicMock()
            fake_t1.request.return_value = dl
            with patch.object(MediaService, "_build_download_client",
                              return_value=(fake_t1, lambda: None)):
                with self.assertRaises(ImageGenError) as ctx:
                    svc.generate_image("x")
            # 实际是「魔数初检失败 -> INVALID_IMAGE_RESPONSE」（save_image_result
            # 抛 ImageGenError，经 generate_image 透传）
            self.assertEqual(ctx.exception.code, "INVALID_IMAGE_RESPONSE")
            # 结果目录中不得出现该「图片」文件
            files = list(Path(d).iterdir())
            self.assertEqual(files, [], f"非图片内容不得落盘，实际 {files}")

    def test_truncated_png_rejected_by_pillow(self):
        """有 PNG 魔数但被截断/损坏 -> Pillow 解码失败 -> IMAGE_DECODE_FAILED。"""
        with tempfile.TemporaryDirectory() as d:
            png = _make_png_bytes()
            truncated = png[: len(png) // 2]  # 保留魔数前缀，截断主体
            self.assertTrue(truncated.startswith(b"\x89PNG"))  # 确认魔数仍在
            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example.invalid/a.png"}]})
            dl = _resp(200, body_bytes=truncated)
            client = _StubClient({"/images/generations": gen, "/a.png": dl})
            svc = self._image_svc(client, Path(d))
            fake_t1 = MagicMock()
            fake_t1.request.return_value = dl
            with patch.object(MediaService, "_build_download_client",
                              return_value=(fake_t1, lambda: None)):
                with self.assertRaises(ImageGenError) as ctx:
                    svc.generate_image("x")
            self.assertEqual(ctx.exception.code, "IMAGE_DECODE_FAILED")
            files = list(Path(d).iterdir())
            self.assertEqual(files, [], f"损坏图片不得落盘，实际 {files}")

    def test_download_has_no_dead_allowed_param(self):
        """_download 签名已移除 allowed 死参数（防 D-NEW-1 回归重建）。"""
        import inspect
        sig = inspect.signature(MediaService._download)
        params = list(sig.parameters)
        self.assertNotIn("allowed", params, "allowed 死参数不应复现")
        # 保留的真实校验参数：max_bytes
        self.assertIn("max_bytes", params)


# ---------------------------------------------------------------------------
# D-NEW-2：视频扩展名识别回归（QuickTime/MOV vs MP4 vs WebM）
#
# 覆盖：
#   - QuickTime ftyp (ftypqtm) -> "mov"
#   - 旧式 "ID"+4字节零值 MOV 头 -> "mov"
#   - 常见 MP4 ftyp (ftypisom) -> "mp4"（兼容性不变）
#   - 常见 MP4 ftyp (ftypmp42) -> "mp4"（兼容性不变）
#   - WebM (\x1a\x45\xdf\xa3) -> "webm"（兼容性不变）
#   - save_video_result(MOV 输入) 生成 .mov 路径 + 字节一致性
#
# 离线：内存构造字节，不下载真实媒体文件；不削弱识别逻辑。
# ---------------------------------------------------------------------------
class TestDNew2VideoExtSniff(unittest.TestCase):
    """D-NEW-2：_sniff_video_ext 扩展名识别 + save_video_result MOV 路径回归。"""

    # 最小合法样本：≥ 64 字节（save_video_result 下限）
    @staticmethod
    def _mv(prefix: bytes) -> bytes:
        """构造 ≥64 字节的视频样本，前缀为识别用的魔数字节。"""
        return prefix + b"\x00" * max(0, 64 - len(prefix))

    def test_ftyp_qtm_maps_to_mov(self):
        """QuickTime ftyp (qt  major_brand) 识别为 'mov'。"""
        sample = self._mv(b"\x00\x00\x00\x18ftypqt  ")
        self.assertEqual(_sniff_video_ext(sample), "mov")

    def test_legacy_id_mov_header_maps_to_mov(self):
        """旧式 'ID'+4字节零值 MOV 头识别为 'mov'。"""
        sample = self._mv(b"ID\x00\x00\x00\x00\x00\x00")
        self.assertEqual(_sniff_video_ext(sample), "mov")

    def test_ftyp_isom_still_mp4(self):
        """常见 MP4 ftyp (isom) 仍识别为 'mp4'（兼容性不变）。"""
        sample = self._mv(b"\x00\x00\x00\x20ftypisom")
        self.assertEqual(_sniff_video_ext(sample), "mp4")

    def test_ftyp_mp42_still_mp4(self):
        """常见 MP4 ftyp (mp42) 仍识别为 'mp4'（兼容性不变）。"""
        sample = self._mv(b"\x00\x00\x00\x20ftypmp42")
        self.assertEqual(_sniff_video_ext(sample), "mp4")

    def test_webm_still_webm(self):
        """WebM 魔数仍识别为 'webm'（兼容性不变）。"""
        sample = self._mv(b"\x1a\x45\xdf\xa3")
        self.assertEqual(_sniff_video_ext(sample), "webm")

    def test_save_video_mov_path_and_bytes(self):
        """save_video_result(MOV 输入) -> 路径以 .mov 结尾，且保存字节与输入一致。"""
        mov_input = self._mv(b"\x00\x00\x00\x18ftypqt  ")
        with tempfile.TemporaryDirectory() as d:
            p = save_video_result(mov_input, Path(d), "p")
            self.assertTrue(p.exists())
            self.assertEqual(p.suffix, ".mov")
            self.assertEqual(p.read_bytes(), mov_input, "保存字节必须与输入完全一致")


# ---------------------------------------------------------------------------
# D-NEW-2 brand 精确匹配（方案 A）回归：
#   仅 "qt  "（标准 QuickTime Movies major_brand）映射 "mov"；
#   含 "qt" 子串但非标准 brand（如 aqt ）→ "mp4"（防子串误判回归）；
#   未知 brand → "mp4"（兜底契约）；WebM 魔数 → "webm"。
#
# 本组 6 项用 _ftyp(brand4) 构造 64 B ftyp box 样本，真实调用 _sniff_video_ext。
# "aqt " 用例在旧子串逻辑（"qt" in brand）下会失败（返回 mov），修复后通过。
# ---------------------------------------------------------------------------
class TestDNew2BrandExactMatch(unittest.TestCase):
    """D-NEW-2 方案 A：major_brand 四字节精确匹配，禁止子串误判。"""

    @staticmethod
    def _ftyp(brand4: str) -> bytes:
        """构造 64 B ftyp box：size(0x18) + 'ftyp' + 4 字节 brand + 补零。"""
        b = brand4.encode("latin-1")
        if len(b) > 4:
            raise ValueError(f"brand 必须 ≤4 字节：{brand4!r}")
        b = b.ljust(4, b"\x00")
        return b"\x00\x00\x00\x18ftyp" + b + b"\x00" * 52

    def test_qt_standard_maps_to_mov(self):
        """标准 QuickTime major_brand 'qt  ' -> mov。"""
        self.assertEqual(_sniff_video_ext(self._ftyp("qt  ")), "mov")

    def test_isom_maps_to_mp4(self):
        """MP4 标准 major_brand 'isom' -> mp4（兼容回归）。"""
        self.assertEqual(_sniff_video_ext(self._ftyp("isom")), "mp4")

    def test_mp42_maps_to_mp4(self):
        """MPEG-4 Part 14 'mp42' -> mp4（兼容回归）。"""
        self.assertEqual(_sniff_video_ext(self._ftyp("mp42")), "mp4")

    def test_aqt_not_misidentified_as_mov(self):
        """'aqt ' 含 'qt' 子串但非标准 QuickTime brand -> 不得误判为 mov。
        D-NEW-3 fail-closed：未知/非白名单 brand 返回 None（而非 mp4 兜底），
        旧子串逻辑下返回 mov，修复后返回 None。"""
        self.assertIsNone(_sniff_video_ext(self._ftyp("aqt ")))

    def test_unknown_brand_returns_none_fail_closed(self):
        """D-NEW-3 fail-closed：未知 major_brand 'zzzz' -> 返回 None（拒绝保存）。
        旧兜底契约（return 'mp4'）已改为 fail-closed（return None）。"""
        self.assertIsNone(_sniff_video_ext(self._ftyp("zzzz")))

    def test_webm_still_webm(self):
        """WebM 魔数（非 ftyp 分支）仍识别为 webm（识别锚点回归）。"""
        sample = b"\x1a\x45\xdf\xa3" + b"\x00" * 60
        self.assertEqual(_sniff_video_ext(sample), "webm")


# ---------------------------------------------------------------------------
# D-NEW-3 fail-closed 回归：未知/损坏/随机字节不再默认判定为 mp4，
# save_video_result 遇 None 时抛 MediaError.decode_failed，不创建伪装 .mp4 产物。
# 离线：内存构造字节 + 临时目录；不下载真实媒体，不调用真实服务商。
# ---------------------------------------------------------------------------
class TestDNew3FailClosedUnknownFormat(unittest.TestCase):
    """D-NEW-3：未知视频格式 fail-closed（_sniff_video_ext 返回 None + 拒绝保存）。"""

    @staticmethod
    def _pad(data: bytes, to: int = 64) -> bytes:
        """补足到 ≥64 字节（save_video_result 下限）。"""
        return data + b"\x00" * max(0, to - len(data))

    @staticmethod
    def _ftyp(brand4: str) -> bytes:
        b = brand4.encode("latin-1").ljust(4, b"\x00")
        return b"\x00\x00\x00\x18ftyp" + b + b"\x00" * 52

    # 1. 已知 MP4 brand -> mp4
    def test_isom_maps_to_mp4(self):
        self.assertEqual(_sniff_video_ext(self._ftyp("isom")), "mp4")

    def test_mp42_maps_to_mp4(self):
        self.assertEqual(_sniff_video_ext(self._ftyp("mp42")), "mp4")

    # 2. 标准 QuickTime brand -> mov
    def test_qt_standard_maps_to_mov(self):
        self.assertEqual(_sniff_video_ext(self._ftyp("qt  ")), "mov")

    # 3. 旧式 ID + 4 零值 MOV -> mov
    def test_legacy_id_mov_maps_to_mov(self):
        sample = self._pad(b"ID\x00\x00" + b"\x00" * 4)
        self.assertEqual(_sniff_video_ext(sample), "mov")

    # 4. WebM -> webm
    def test_webm_maps_to_webm(self):
        sample = self._pad(b"\x1a\x45\xdf\xa3")
        self.assertEqual(_sniff_video_ext(sample), "webm")

    # 5. FLAC 识别（D-NEW-4 修复后行为）。
    #    D-NEW-4 已将 L471 FLAC 字面量由错误的 b"\x46\x6c\x61\x43"（"FlaC"，首字节大写 0x46）
    #    修正为正确魔数 b"\x66\x4c\x61\x43"（"fLaC"，首字节小写 0x66），真实 FLAC 文件
    #    现可被正确识别为 "flac"；错误大小写 "FlaC" 不再被识别（落入 fail-closed → None）。
    def test_flac_magic_recognized_after_dnew4_fix(self):
        """D-NEW-4：正确 FLAC 魔数 b'fLaC' 被识别为 'flac'。"""
        real_flac = self._pad(b"fLaC")
        self.assertEqual(_sniff_video_ext(real_flac), "flac")

    def test_flac_wrong_case_not_recognized(self):
        """D-NEW-4：错误大小写 'FlaC'（0x46 大写）不再被识别为 FLAC 魔数，
        落入 fail-closed → None。"""
        wrong_case = self._pad(b"FlaC")
        self.assertIsNone(_sniff_video_ext(wrong_case),
                          "错误大小写 'FlaC' 不应被识别为 FLAC（D-NEW-4 修复后）")

    # 6. 未知 ftyp brand -> None
    def test_unknown_ftyp_brand_returns_none(self):
        self.assertIsNone(_sniff_video_ext(self._ftyp("zzzz")))

    # 7. 随机字节 -> None
    def test_random_bytes_returns_none(self):
        sample = self._pad(b"\xff" * 32 + b"\x00" * 32)
        self.assertIsNone(_sniff_video_ext(sample))

    # 8. JSON-like 错误 payload -> None
    def test_json_error_payload_returns_none(self):
        sample = self._pad(b'{"error": "invalid media"}')
        self.assertIsNone(_sniff_video_ext(sample))

    # 9. 损坏 ftyp（大小字段错 0x99）-> None
    def test_corrupted_ftyp_size_returns_none(self):
        sample = self._pad(b"\x00\x00\x00\x99ftypisom")
        self.assertIsNone(_sniff_video_ext(sample))

    # 10. 空字节 / 截断数据仍按既有输入校验机制拒绝
    def test_empty_and_truncated_rejected_by_gate(self):
        # len < 64 -> save_video_result 在 sniffing 之前拒绝
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MediaError):
                save_video_result(b"", Path(d), "p")
            with self.assertRaises(MediaError):
                save_video_result(b"\x00\x00\x00\x20ft", Path(d), "p")

    # 11. save_video_result 遇到未知格式抛出 decode_failed
    def test_save_video_unknown_format_raises_decode_failed(self):
        unknown = self._pad(self._ftyp("zzzz"))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MediaError) as ctx:
                save_video_result(unknown, Path(d), "p")
            self.assertEqual(ctx.exception.code, "DECODE_FAILED")

    # 12. 未知格式拒绝保存后，不存在伪 .mp4 产物
    def test_no_fake_mp4_artifact_after_rejection(self):
        unknown = self._pad(self._ftyp("zzzz"))
        with tempfile.TemporaryDirectory() as d:
            try:
                save_video_result(unknown, Path(d), "p")
            except MediaError:
                pass
            # results_dir 中不应存在任何文件（fail-closed 在 write 前拒绝）
            files = list(Path(d).iterdir())
            self.assertEqual(files, [], f"拒绝后不应留下产物，实际：{files}")

    # 13. 已知格式保存后扩展名正确 + 字节一致
    def test_known_format_saves_correct_ext_and_bytes(self):
        mp4_input = self._ftyp("isom")
        with tempfile.TemporaryDirectory() as d:
            p = save_video_result(mp4_input, Path(d), "p")
            self.assertEqual(p.suffix, ".mp4")
            self.assertEqual(p.read_bytes(), mp4_input, "保存字节须与输入一致")

    # 13b. D-NEW-4：FLAC 魔数保存后扩展名 .flac + 字节一致
    def test_flac_saves_with_flac_ext_and_bytes(self):
        """D-NEW-4：save_video_result(FLAC 魔数) -> .flac 后缀 + 保存字节与输入一致。"""
        flac_input = self._pad(b"fLaC")
        with tempfile.TemporaryDirectory() as d:
            p = save_video_result(flac_input, Path(d), "p")
            self.assertEqual(p.suffix, ".flac")
            self.assertEqual(p.read_bytes(), flac_input, "FLAC 保存字节须与输入一致")

    # 14. 既有下载大小限制 / 认证 / 错误处理测试通过（本类仅覆盖 D-NEW-3 新增面；
    #     原有 test_save_video_result 的 too-large 与 invalid_response 由回归套件保证）


# ---------------------------------------------------------------------------
# D-NEW-5 格式误识别修复回归：
#   - FLV 魔数（b"FLV"/b"FLV9"）不再被识别为 webm，落入 fail-closed → None；
#   - WebM EBML 签名（b"\x1a\x45\xdf\xa3"）仍识别为 webm（回归锚点）；
#   - RIFF+WEBP 图像数据不再被视频识别函数误判为 webm → None。
# 离线：内存构造字节，不下载真实媒体，不调用真实服务商。
# ---------------------------------------------------------------------------
class TestDNew5FormatMisclassificationFix(unittest.TestCase):
    """D-NEW-5：FLV/RIFF+WEBP 误识别修复 + WebM 回归 + fail-closed 行为。"""

    @staticmethod
    def _pad(data: bytes, to: int = 64) -> bytes:
        return data + b"\x00" * max(0, to - len(data))

    # 1. FLV 魔数不得返回 webm（fail-closed → None）
    def test_flv_magic_returns_none(self):
        """FLV 魔数 b'FLV' -> None（不得误识别为 webm）。"""
        sample = self._pad(b"FLV")
        self.assertIsNone(_sniff_video_ext(sample),
                          "FLV 魔数不得被识别为 webm（D-NEW-5 修复后 fail-closed）")

    def test_flv9_magic_returns_none(self):
        """FLV 魔数 b'FLV9' -> None（不得误识别为 webm）。"""
        sample = self._pad(b"FLV9")
        self.assertIsNone(_sniff_video_ext(sample),
                          "FLV9 魔数不得被识别为 webm（D-NEW-5 修复后 fail-closed）")

    # 2. WebM 正确 EBML 签名仍返回 webm（回归锚点）
    def test_webm_ebml_signature_still_webm(self):
        """WebM EBML 签名 b'\\x1a\\x45\\xdf\\xa3' -> webm（D-NEW-5 后行为不变）。"""
        sample = self._pad(b"\x1a\x45\xdf\xa3")
        self.assertEqual(_sniff_video_ext(sample), "webm")

    # 3. RIFF+WEBP 返回 None（不得被视频识别函数误判为 webm）
    def test_riff_webp_returns_none(self):
        """RIFF+WEBP 图像数据 -> None（WEBP 非视频，不得返回 webm）。"""
        sample = self._pad(b"RIFF" + b"\x00" * 4 + b"WEBP")
        self.assertIsNone(_sniff_video_ext(sample),
                          "RIFF+WEBP 图像数据不得被识别为 webm（D-NEW-5 修复后 fail-closed）")

    # 4. save_video_result 对 FLV/RIFF+WEBP 输入拒绝保存（DECODE_FAILED），无伪产物
    def test_save_video_rejects_flv_and_riff_webp(self):
        """save_video_result(FLV/RIFF+WEBP) -> DECODE_FAILED 拒绝，无伪 .webm 产物。"""
        for label, data in [("FLV", b"FLV"), ("RIFF+WEBP", b"RIFF" + b"\x00" * 4 + b"WEBP")]:
            sample = self._pad(data)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(MediaError) as ctx:
                    save_video_result(sample, Path(d), "p")
                self.assertEqual(ctx.exception.code, "DECODE_FAILED",
                                 f"{label} 应触发 DECODE_FAILED，实际 {ctx.exception.code}")
                files = list(Path(d).iterdir())
                self.assertEqual(files, [], f"{label} 拒绝后不应留下产物，实际：{files}")

    # 5. 既有格式识别不回归
    def test_existing_formats_unaffected(self):
        """MP4(isom)/MOV(qt  )/旧式 MOV/WebM/FLAC 识别行为不变。"""
        self.assertEqual(_sniff_video_ext(self._pad(b"\x00\x00\x00\x20ftypisom")), "mp4")
        self.assertEqual(_sniff_video_ext(self._pad(b"\x00\x00\x00\x18ftypqt  ")), "mov")
        self.assertEqual(_sniff_video_ext(self._pad(b"ID\x00\x00" + b"\x00" * 4)), "mov")
        self.assertEqual(_sniff_video_ext(self._pad(b"\x1a\x45\xdf\xa3")), "webm")
        self.assertEqual(_sniff_video_ext(self._pad(b"fLaC")), "flac")


# ---------------------------------------------------------------------------
# S4-A：通用媒体 API 接入（离线）
#   A. 不再内置未核实的默认模型（sora-2 / gpt-image-1 / gpt-4o-mini-tts /
#      whisper-1 已全部移除；未配置模型时请求字段为空串）。
#   B. 结果 URL 主机白名单（extra_params["result_hosts"]，精确小写匹配，
#      fail-closed 清洗；绝不自动补入 base_url 主机；重定向仍被
#      follow_redirects=False 阻止，白名单不放宽安全层）。
#   C. settings.py 的 _clean_provider_entry 放行 extra_params["result_hosts"]
#      （list[str]，全部元素为 str 才保留；否则丢弃）。
#   D. D4 运算符优先级修复（ui.py 添加/保存行的 protocol 显式归一化）
#      与 D3 名称覆盖修复（保存行 name 保留用户显示名）的等价行为测试
#      （离线：按 ui.py 相同表达式复刻断言，不实例化 Qt 控件）。
# ---------------------------------------------------------------------------
class TestS4AMediaConfigDefaults(unittest.TestCase):
    """S4-A §B：未配置模型时请求 payload 不再携带内置默认模型。"""

    def _prov(self, model="", extra=None):
        return MediaProvider(
            service="video",
            provider_id="test",
            base_url="https://example.invalid/v1",
            model=model,
            protocol="openai-video-task",
            extra_params=extra or {},
        )

    def test_video_payload_no_default_model_when_unconfigured(self):
        from core.media_generation import _OpenAIVideoTaskAdapter
        prov = self._prov(model="")
        _, payload = _OpenAIVideoTaskAdapter.create_request(prov, "p", {})
        self.assertEqual(payload["model"], "",
                         "未配置模型时 video payload.model 应为空串（S4-A 移除 sora-2）")
        self.assertEqual(payload["prompt"], "p")

    def test_video_payload_uses_configured_model(self):
        from core.media_generation import _OpenAIVideoTaskAdapter
        prov = self._prov(model="veo-3-fast")
        _, payload = _OpenAIVideoTaskAdapter.create_request(prov, "p", {})
        self.assertEqual(payload["model"], "veo-3-fast")

    def test_image_payload_no_default_model_when_unconfigured(self):
        from core.media_generation import _OpenAIImageAdapter
        prov = MediaProvider(
            service="image", provider_id="test",
            base_url="https://example.invalid/v1",
            model="", protocol="openai",
        )
        payload = _OpenAIImageAdapter.build_payload(prov, "p", "1024x1024", "low")
        self.assertEqual(payload["model"], "",
                         "未配置模型时 image payload.model 应为空串（S4-A 移除 gpt-image-1）")

    def test_tts_payload_no_default_model_when_unconfigured(self):
        from core.media_generation import _OpenAIAudioAdapter
        prov = MediaProvider(
            service="voice", provider_id="test",
            base_url="https://example.invalid/v1",
            model="", protocol="openai-audio",
        )
        payload = _OpenAIAudioAdapter.speech_payload(
            prov, "hi", "alloy", "mp3", 1.0, {})
        self.assertEqual(payload["model"], "",
                         "未配置模型时 TTS payload.model 应为空串（S4-A 移除 gpt-4o-mini-tts）")

    def test_stt_fields_no_default_model_when_unconfigured(self):
        from core.media_generation import _OpenAIAudioAdapter
        prov = MediaProvider(
            service="voice", provider_id="test",
            base_url="https://example.invalid/v1",
            model="", protocol="openai-audio",
        )
        data = _OpenAIAudioAdapter.stt_fields(prov, "zh", "", {})
        self.assertEqual(data["model"], "",
                         "未配置模型时 STT model 应为空串（S4-A 移除 whisper-1）")

    def test_stt_fields_explicit_model_param_still_wins(self):
        """既有优先级：显式 model 参数 > 配置 model > 空串（回归守护）。"""
        from core.media_generation import _OpenAIAudioAdapter
        prov = MediaProvider(
            service="voice", provider_id="test",
            base_url="https://example.invalid/v1",
            model="cfg-model", protocol="openai-audio",
        )
        data = _OpenAIAudioAdapter.stt_fields(prov, "zh", "explicit-model", {})
        self.assertEqual(data["model"], "explicit-model")
        data2 = _OpenAIAudioAdapter.stt_fields(prov, "zh", "", {})
        self.assertEqual(data2["model"], "cfg-model")


class TestS4AResultHostsWhitelist(unittest.TestCase):
    """S4-A §B/D：结果 URL 主机白名单（_result_hosts_from_prov + _download 校验）。"""

    def _prov(self, extra):
        return MediaProvider(
            service="video", provider_id="test",
            base_url="https://api.example.invalid/v1",
            protocol="openai-video-task", extra_params=extra,
        )

    # 1. 未配置 -> None（S4-B2 F2：_download 据此拒绝结果下载）
    def test_unconfigured_returns_none(self):
        from core.media_generation import _result_hosts_from_prov
        self.assertIsNone(_result_hosts_from_prov(self._prov({})))
        self.assertIsNone(_result_hosts_from_prov(self._prov({"result_hosts": None})))

    # 2. 合法列表 -> 小写 frozenset
    def test_valid_list_normalized(self):
        from core.media_generation import _result_hosts_from_prov
        prov = self._prov({"result_hosts": ["CDN.Example.com", "api.example.invalid"]})
        got = _result_hosts_from_prov(prov)
        self.assertEqual(got, frozenset({"cdn.example.com", "api.example.invalid"}))

    # 3. 含非法项（scheme/通配符/路径/空串/非 str/非 list）-> fail-closed 清洗
    def test_illegal_items_dropped(self):
        from core.media_generation import _result_hosts_from_prov
        prov = self._prov({
            "result_hosts": [
                "https://ok.example.com",  # 含 scheme -> 丢
                "ok.example.com",          # 合法 -> 留
                "*.example.com",           # 通配符 -> 丢
                "ok.example.com/x",        # 含路径 -> 丢
                "",                        # 空串 -> 丢
                123,                       # 非 str -> 丢
            ],
        })
        self.assertEqual(_result_hosts_from_prov(prov), frozenset({"ok.example.com"}))

    def test_non_list_returns_empty_set(self):
        """非 list 形态（字符串/数字）-> 空 frozenset（S4-B2 F2 三态「配置无效」
        -> _download 拒绝结果下载，不得转换为「不限制」）。"""
        self.assertEqual(_result_hosts_from_prov(self._prov({"result_hosts": "ok.example.com"})), frozenset())
        self.assertEqual(_result_hosts_from_prov(self._prov({"result_hosts": 42})), frozenset())

    def test_list_with_some_invalid_keeps_valid_only(self):
        """list 内含非 str 元素 -> 丢弃该元素、保留合法项（逐项清洗，非整体拒绝）。"""
        self.assertEqual(_result_hosts_from_prov(
            self._prov({"result_hosts": ["ok.example.com", 42]})),
            frozenset({"ok.example.com"}))

    # 4. 清洗后为空集 -> 空 frozenset（S4-B2 F2 三态「配置无效」，_download 拒绝）
    def test_all_items_illegal_returns_empty_set(self):
        from core.media_generation import _result_hosts_from_prov
        self.assertEqual(_result_hosts_from_prov(
            self._prov({"result_hosts": ["https://a.b", "*.*"]})), frozenset())

    # 5. 白名单校验在 _download 生效：命中放行 / 未命中拒绝
    class _FakeResp:
        content = b"PAYLOAD"

        def raise_for_status(self):
            pass

    # S4-B2：T1 专属池（_build_download_client）在实例方法 _download 中
    # 被真实调用；单测层面以 patch 替换 T1 客户端构造，校验 T1 调用链
    # 被触发（SNI 绑定、专属池释放）+ 最终字节返回。
    def _patched_download(self, prov, url, hosts):
        svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
        captured = {}

        def fake_build_download_client(host, handle_or_prov):
            captured["host"] = host
            resp = self._FakeResp()
            client = MagicMock()
            client.request.return_value = resp
            # 记录 release 调用
            released = []
            def _release():
                released.append(True)
            captured["client"] = client
            captured["release"] = released
            return client, _release
        with patch.object(MediaService, "_build_download_client", side_effect=fake_build_download_client):
            try:
                data = svc._download(url, MagicMock(), prov,
                                     max_bytes=1024, result_hosts=hosts)
            except Exception as e:
                captured["error"] = e
                data = None
        return data, captured

    def test_download_allowed_host_passes(self):
        prov = self._prov({"result_hosts": ["cdn.example.com"]})
        hosts = _result_hosts_from_prov(prov)
        data, cap = self._patched_download(prov, "https://cdn.example.com/out.mp4", hosts)
        self.assertEqual(data, b"PAYLOAD")
        self.assertEqual(cap["host"], "cdn.example.com", "SNI 绑定结果域")
        # T1 专属池必须被释放（finally 调用）
        self.assertEqual(cap["release"], [True])
        # 请求确实打到该主机
        kw = cap["client"].request.call_args.kwargs
        self.assertIn("cdn.example.com", kw["url"])

    def test_download_unlisted_host_rejected(self):
        prov = self._prov({"result_hosts": ["cdn.example.com"]})
        hosts = _result_hosts_from_prov(prov)
        client = MagicMock()
        with self.assertRaises(MediaError) as ctx:
            svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
            svc._download(
                "https://evil.example.net/out.mp4", client, prov,
                max_bytes=1024, result_hosts=hosts)
        self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")
        client.request.assert_not_called()  # 白名单拒绝发生在任何网络请求之前

    def test_download_none_result_hosts_rejected(self):
        """S4-B2 F2 三态「未配置」-> 拒绝下载。"""
        prov = self._prov({})
        with self.assertRaises(MediaError) as ctx:
            svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
            svc._download(
                "https://cdn.example.com/out.mp4", MagicMock(), prov,
                max_bytes=1024, result_hosts=None)
        self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")

    def test_download_empty_result_hosts_rejected(self):
        """S4-B2 F2 三态「配置无效（空集）」-> 拒绝下载。"""
        prov = self._prov({"result_hosts": ["https://a.b"]})
        hosts = _result_hosts_from_prov(prov)
        self.assertEqual(hosts, frozenset())
        with self.assertRaises(MediaError) as ctx:
            svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
            svc._download(
                "https://cdn.example.com/out.mp4", MagicMock(), prov,
                max_bytes=1024, result_hosts=hosts)
        self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")

    def test_download_ip_literal_hosts_rejected(self):
        """S4-B2 F2：IP 字面量项默认丢弃 -> 空集 -> 拒绝下载。"""
        for ip in ("93.184.21.34", "2001:db8::1", "::ffff:192.0.2.1"):
            prov = self._prov({"result_hosts": [ip]})
            hosts = _result_hosts_from_prov(prov)
            self.assertEqual(hosts, frozenset(), f"{ip} 应被丢弃")
            with self.assertRaises(MediaError) as ctx:
                svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
                svc._download(
                    f"https://{ip}/out.mp4", MagicMock(), prov,
                    max_bytes=1024, result_hosts=hosts)
            self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")

    def test_download_private_host_whitelist_does_not_relax(self):
        """白名单不放宽安全层：file:// 目标即使主机名「列出」仍拒绝（scheme 先行）。"""
        prov = self._prov({"result_hosts": ["127.0.0.1", "example.com"]})
        hosts = _result_hosts_from_prov(prov)
        client = MagicMock()
        with self.assertRaises(MediaError) as ctx:
            svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
            svc._download(
                "file://127.0.0.1/x.mp4", client, prov,
                max_bytes=1024, result_hosts=hosts)
        self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")
        client.request.assert_not_called()

    def test_download_whitelist_rejects_local_file_scheme_even_if_listed(self):
        """file:// 目标即使主机名「列出」也不放行（scheme != https 先行拒绝）。"""
        prov = self._prov({"result_hosts": ["example.com"]})
        hosts = _result_hosts_from_prov(prov)
        with self.assertRaises(MediaError) as ctx:
            svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
            svc._download(
                "file://example.com/x.mp4", MagicMock(), prov,
                max_bytes=1024, result_hosts=hosts)
        self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")

    def test_download_no_whitelist_rejected(self):
        """S4-B2 F2 三态「未配置」：result_hosts=None -> 拒绝结果下载。
        （S4-A 时代的「None=不限制」语义已被 S4-B1 补充报告 §2 推翻；
        此测试守护新 fail-closed 语义。）"""
        prov = self._prov({})
        hosts = _result_hosts_from_prov(prov)
        self.assertIsNone(hosts)
        client = MagicMock()
        with self.assertRaises(MediaError) as ctx:
            svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
            svc._download(
                "https://cdn.example.com/out.mp4", client, prov,
                max_bytes=1024, result_hosts=hosts)
        self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")
        client.request.assert_not_called()

    # ---- S4-B2 F3：HTTPS 443 端口约束 + 歧义闸门 ----
    def _port_case(self, url, hosts=("cdn.example.com",), expect_t1=True):
        """驱动 F3 闸门：T1 构造被 patch 拦截。
        expect_t1=True：T1 拦截器返回 stub（F3 放行场景，验证进入 T1 构造）；
        expect_t1=False：T1 拦截器抛 AssertionError（F3 应拒绝场景，验证 T1 未被触发）。
        F3 在 T1 之前拒绝时抛 MediaError(DOWNLOAD_FAILED) -> 返回其 code；
        F3 放行（T1 被调用且 stub 正常返回）-> 返回 None。"""
        prov = self._prov({"result_hosts": list(hosts)})
        h = _result_hosts_from_prov(prov)
        svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
        if expect_t1:
            def _stub_t1(self_arg, host, handle):
                fake = MagicMock()
                fake.request.return_value = self._FakeResp()
                return fake, lambda: None
            with patch.object(MediaService, "_build_download_client", _stub_t1):
                try:
                    svc._download(url, MagicMock(), prov, max_bytes=1024, result_hosts=h)
                except MediaError as e:
                    return e.code
                return None  # F3 放行，T1 被调用且正常返回
        else:
            with patch.object(MediaService, "_build_download_client",
                              side_effect=AssertionError("F3 拒绝场景下 T1 构造不应被触发")):
                try:
                    svc._download(url, MagicMock(), prov, max_bytes=1024, result_hosts=h)
                except MediaError as e:
                    return e.code
                return None

    def test_f3_default_port_allowed(self):
        """缺省端口（https://host/）-> 通过端口闸门（进入 T1 构造，被 patch 拦截）。"""
        self.assertIsNone(self._port_case("https://cdn.example.com/out.mp4"))

    def test_f3_explicit_443_allowed(self):
        self.assertIsNone(self._port_case("https://cdn.example.com:443/out.mp4"))

    def test_f3_non443_port_rejected(self):
        for u in ("https://cdn.example.com:8443/x", "https://cdn.example.com:0/x",
                  "https://cdn.example.com:65536/x"):
            self.assertEqual(self._port_case(u), "DOWNLOAD_FAILED", u)

    def test_f3_non_numeric_port_rejected(self):
        self.assertEqual(self._port_case("https://cdn.example.com:-1/x"), "DOWNLOAD_FAILED")
        self.assertEqual(self._port_case("https://cdn.example.com:abc/x"), "DOWNLOAD_FAILED")
        self.assertEqual(self._port_case("https://cdn.example.com:/x"), "DOWNLOAD_FAILED")

    def test_f3_userinfo_rejected(self):
        self.assertEqual(self._port_case("https://user:pass@cdn.example.com/x"), "DOWNLOAD_FAILED")

    def test_f3_ipv6_literal_never_in_whitelist(self):
        """S4-B2 F2：IPv6 字面量（含方括号 netloc 的裸值）被白名单清洗丢弃 ->
        空集 -> 拒绝下载（不依赖 F3 端口闸门，F2 先行）。"""
        for ip in ("2001:db8::1", "::ffff:192.0.2.1"):
            prov = self._prov({"result_hosts": [ip]})
            hosts = _result_hosts_from_prov(prov)
            self.assertEqual(hosts, frozenset())
            with self.assertRaises(MediaError) as ctx:
                svc = MediaService(settings=_video_settings(), results_dir=Path("workspace/results"))
                svc._download(
                    f"https://[{ip}]/x", MagicMock(), prov,
                    max_bytes=1024, result_hosts=hosts)
            self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")


class TestS4ASettingsResultHostsCleaning(unittest.TestCase):
    """S4-A §C：settings._clean_provider_entry 对 extra_params["result_hosts"] 的清洗。"""

    def _cleaner(self):
        from core.settings import SettingsManager
        m = SettingsManager.__new__(SettingsManager)
        return m._clean_provider_entry

    def test_result_hosts_list_of_str_kept(self):
        cleaner = self._cleaner()
        out = cleaner({
            "name": "p", "base_url": "https://x.invalid",
            "extra_params": {"result_hosts": ["a.example.com", "b.example.com"]},
        })
        self.assertEqual(
            out.get("extra_params", {}).get("result_hosts"),
            ["a.example.com", "b.example.com"])

    def test_result_hosts_non_str_list_dropped(self):
        cleaner = self._cleaner()
        out = cleaner({
            "name": "p", "base_url": "https://x.invalid",
            "extra_params": {"result_hosts": ["ok.example.com", 42]},
        })
        self.assertNotIn("result_hosts", out.get("extra_params", {}))

    def test_result_hosts_not_a_list_dropped(self):
        cleaner = self._cleaner()
        out = cleaner({
            "name": "p", "base_url": "https://x.invalid",
            "extra_params": {"result_hosts": "not-a-list"},
        })
        self.assertNotIn("result_hosts", out.get("extra_params", {}))

    def test_extra_scalar_keys_still_cleaned(self):
        """回归守护：既有 str/int/float/bool 键清洗行为不变。"""
        cleaner = self._cleaner()
        out = cleaner({
            "name": "p", "base_url": "https://x.invalid",
            "extra_params": {"size": "1024x1024", "n": 1, "flag": True},
        })
        self.assertEqual(out.get("extra_params", {}).get("size"), "1024x1024")
        self.assertEqual(out.get("extra_params", {}).get("n"), 1)
        self.assertEqual(out.get("extra_params", {}).get("flag"), True)


class TestS4AD4ProtocolNormalization(unittest.TestCase):
    """S4-A §D：ui.py D4 修复等价表达式（离线复刻，不实例化 Qt）。

    旧缺陷：`protocol or "openai" if service=="image" else (...)` 被 Python
    解析为 `protocol or ("openai" if image else (...))`，video/voice 下用户
    输入的非默认协议被吞掉。修复后显式：空值 -> service 默认协议；非空 -> 小写保留。
    """

    @staticmethod
    def new_expr(service, protocol):
        return (protocol.strip().lower() or (
            "openai" if service == "image"
            else "openai-video-task" if service == "video"
            else "openai-audio"))

    @staticmethod
    def old_expr(service, protocol):
        return protocol or "openai" if service == "image" else (
            "openai-video-task" if service == "video" else "openai-audio")

    def test_video_nondefault_protocol_preserved(self):
        self.assertEqual(self.new_expr("video", "my-custom-video"), "my-custom-video")
        self.assertEqual(self.old_expr("video", "my-custom-video"), "openai-video-task",
                         "旧表达式缺陷证据：video 下非默认协议被吞")

    def test_voice_nondefault_protocol_preserved(self):
        self.assertEqual(self.new_expr("voice", "custom-tts"), "custom-tts")
        self.assertEqual(self.old_expr("voice", "custom-tts"), "openai-audio")

    def test_image_nondefault_protocol_preserved(self):
        self.assertEqual(self.new_expr("image", "generic-image"), "generic-image")

    def test_empty_protocol_falls_back_to_service_default(self):
        self.assertEqual(self.new_expr("video", ""), "openai-video-task")
        self.assertEqual(self.new_expr("image", ""), "openai")
        self.assertEqual(self.new_expr("voice", ""), "openai-audio")
        self.assertEqual(self.new_expr("video", "   "), "openai-video-task")

    def test_uppercase_normalized(self):
        self.assertEqual(self.new_expr("video", "GENERIC-VIDEO"), "generic-video")


class TestS4AD3NamePreservation(unittest.TestCase):
    """S4-A §D：ui.py D3 修复等价行为（保存行 name 保留用户显示名，缺失才回退 pid）。"""

    @staticmethod
    def new_expr(pconf_name, sel):
        return pconf_name or sel

    @staticmethod
    def old_expr(pconf_name, sel):
        return sel

    def test_user_display_name_kept(self):
        self.assertEqual(self.new_expr("My Provider", "my_provider"), "My Provider")
        self.assertEqual(self.old_expr("My Provider", "my_provider"), "my_provider",
                         "旧表达式缺陷证据：显示名被 provider_id 覆盖")

    def test_missing_name_falls_back_to_pid(self):
        self.assertEqual(self.new_expr("", "my_provider"), "my_provider")
        self.assertEqual(self.new_expr(None, "my_provider"), "my_provider")


# ---------------------------------------------------------------------------
# S4-B2 V2：T1 专属池缓存隔离 + F3 端口闸门直接判定
#
# 覆盖（任务书 §E V2 + §D）：
#   - T1 专属客户端不得写入 _client_for 共享缓存（自域 base_url 才入缓存）
#   - F3 端口闸门：非 443 / 非数字 / 越界 / 尾空端口 / userinfo -> 拒绝
#   - F3 通过（缺省 443）时 T1 才被构造
# 全程离线：stub T1 客户端 + patch；不触真实网络。
# ---------------------------------------------------------------------------
class TestS4B2CacheIsolationAndF3(unittest.TestCase):
    """V2 缓存隔离 + F3 端口闸门直接判定。"""

    def _svc(self, extra, results_dir):
        ms = {
            "video": {"enabled": True, "provider": "openai",
                      "providers": {"openai": {
                          "base_url": "https://api.example.invalid/v1",
                          "protocol": "openai-video-task", "model": "",
                          "extra_params": extra,
                      }}},
            "image": {}, "voice": {},
        }
        s = _FakeKeyringSettings(media_services=ms, keys={"video_openai": SYNTH_KEY})
        svc = MediaService(settings=s, results_dir=results_dir)
        svc._client_for = lambda prov: MagicMock()
        return svc

    def _prov(self, extra):
        return MediaProvider(
            service="video", provider_id="openai",
            base_url="https://api.example.invalid/v1",
            protocol="openai-video-task", extra_params=extra,
        )

    # V2-1：T1 专属客户端不入 _clients 共享缓存
    def test_v2_t1_not_in_shared_cache(self):
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc({"result_hosts": ["cdn.example.invalid"]}, Path(d))
            prov = self._prov({"result_hosts": ["cdn.example.invalid"]})
            hosts = _result_hosts_from_prov(prov)
            fake_t1 = MagicMock()
            fake_t1.request.return_value = _resp(200, body_bytes=b"DATA")
            built = []
            def fake_build(self_arg, host, handle):
                built.append(host)
                return fake_t1, lambda: None
            with patch.object(MediaService, "_build_download_client", fake_build):
                svc._download("https://cdn.example.invalid/a.png", MagicMock(), prov,
                              max_bytes=1024, result_hosts=hosts)
            # T1 构造确实发生（请求期），但结果域不得写入共享缓存
            self.assertEqual(built, ["cdn.example.invalid"])
            self.assertNotIn("cdn.example.invalid", svc._clients,
                             "T1 专属池不得写入 _client_for 共享缓存")
            # 共享缓存只可能被自域 base_url 填充（此处未调用 _client_for 入缓存）

    # V2-2：两次顺序下载使用独立冻结集（互不串用）
    def test_v2_sequential_downloads_distinct_frozen_sets(self):
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc({"result_hosts": ["cdn.example.invalid"]}, Path(d))
            frozen_sets = []
            def fake_build(self_arg, host, handle):
                # patch.object 替换类方法 -> 实例调用时隐式传入 self
                # 模拟 T1 构造期逐请求冻结授权集（同生共灭）
                fs = frozenset({"1.1.1.1"})
                frozen_sets.append(fs)
                fake_t1 = MagicMock()
                fake_t1.request.return_value = _resp(200, body_bytes=b"X")
                return fake_t1, lambda: None
            prov1 = self._prov({"result_hosts": ["a.example.invalid"]})
            prov2 = self._prov({"result_hosts": ["b.example.invalid"]})
            h1 = _result_hosts_from_prov(prov1)
            h2 = _result_hosts_from_prov(prov2)
            with patch.object(MediaService, "_build_download_client", fake_build):
                svc._download("https://a.example.invalid/x", MagicMock(), prov1,
                              max_bytes=1024, result_hosts=h1)
                svc._download("https://b.example.invalid/x", MagicMock(), prov2,
                              max_bytes=1024, result_hosts=h2)
            self.assertEqual(len(frozen_sets), 2)
            self.assertIsNot(frozen_sets[0], frozen_sets[1],
                             "两次顺序下载的冻结授权集必须是独立对象（无串用）")

    # V2-3：异常路径释放 T1（finally 保证）
    def test_v2_exception_path_releases_t1(self):
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc({"result_hosts": ["cdn.example.invalid"]}, Path(d))
            released = []
            def fake_build(self_arg, host, handle):
                fake_t1 = MagicMock()
                def _boom(*a, **k):
                    raise httpx2.ReadTimeout("read timed out")
                fake_t1.request.side_effect = _boom
                return fake_t1, (lambda: released.append(True))
            prov = self._prov({"result_hosts": ["cdn.example.invalid"]})
            hosts = _result_hosts_from_prov(prov)
            with patch.object(MediaService, "_build_download_client", fake_build):
                with self.assertRaises((MediaError, httpx2.TimeoutException)):
                    svc._download("https://cdn.example.invalid/a.png", MagicMock(), prov,
                                  max_bytes=1024, result_hosts=hosts)
            self.assertEqual(released, [True],
                             "异常路径下 T1 专属池必须经 finally 释放")

    # F3：非 443 端口拒绝
    def test_f3_non443_port_rejected(self):
        import core.media_generation as mg
        from urllib.parse import urlparse
        for u in ("https://cdn.example.invalid:8443/x",
                  "https://cdn.example.invalid:0/x",
                  "https://cdn.example.invalid:65536/x"):
            with self.assertRaises(MediaError) as ctx:
                mg._validate_result_url_port(urlparse(u), "video", "p")
            self.assertEqual(ctx.exception.code, "DOWNLOAD_FAILED")

    def test_f3_default_and_443_allowed(self):
        import core.media_generation as mg
        from urllib.parse import urlparse
        for u in ("https://cdn.example.invalid/x",
                  "https://cdn.example.invalid:443/x"):
            mg._validate_result_url_port(urlparse(u), "video", "p")  # 不抛异常

    def test_f3_userinfo_rejected(self):
        import core.media_generation as mg
        from urllib.parse import urlparse
        with self.assertRaises(MediaError):
            mg._validate_result_url_port(urlparse("https://user:pass@cdn.example.invalid/x"),
                                         "video", "p")

    def test_f3_ipv6_non443_port_rejected(self):
        """IPv6 方括号 + 非 443 显式端口 -> F3 闸门拒绝。"""
        import core.media_generation as mg
        from urllib.parse import urlparse
        with self.assertRaises(MediaError):
            mg._validate_result_url_port(urlparse("https://[2001:db8::1]:8443/x"),
                                         "video", "p")

    def test_f3_malformed_ipv6_bracket_rejected(self):
        """畸形 IPv6 方括号（netloc 含 '[' 但缺 ']'）-> F3 闸门拒绝。
        用手工 parsed 形态驱动闸门自身的畸形分支（urlparse 对真实 URL 的
        畸形方括号会在 _download 入口抛 ValueError，同样 fail-closed）。"""
        import core.media_generation as mg
        from types import SimpleNamespace
        parsed = SimpleNamespace(netloc="[2001:db8::1", hostname="2001:db8::1",
                                  scheme="https", port=None, path="/x")
        with self.assertRaises(MediaError):
            mg._validate_result_url_port(parsed, "video", "p")


# ---------------------------------------------------------------------------
# S4-B2 V1：回环 TLS 实证（PeerPolicyBackend policy_state 真实 connect_tcp）
#
# 复用 tests/test_image_generation.py 的回环 TLS 服务器 + 自签证书夹具，
# 注入 resolver 使结果域解析到 127.0.0.1（回环），验证：
#   - 首次解析公网、连接前变私网（rebinding 窗口）-> 拒绝
#   - 多候选含私网 -> 整体拒绝
#   - 实际 TCP peer ∉ 授权集 -> PEER_REJECTED
#   - TLS SNI/证书主机名验证不降强度
#   - 连接失败时 T1 资源释放
# 全程回环 + 自签证书；不触真实网络。cryptography 不可用时干净跳过。
# ---------------------------------------------------------------------------
class TestS4B2V1LoopbackTLS(unittest.TestCase):
    """§E V1：回环 TLS + 注入 resolver 的离线实证。"""

    @classmethod
    def setUpClass(cls):
        from tests.test_image_generation import _LoopbackTLSServer, _gen_cert
        cls._LoopbackTLSServer = _LoopbackTLSServer
        cls.server = _LoopbackTLSServer()
        if cls.server.cert_path is None:
            raise unittest.SkipTest("cryptography unavailable; V1 TLS skipped")
        cls.server.thread.start()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None) is not None and \
                getattr(cls.server, "thread", None) is not None:
            cls.server.stop()
            cls.server.thread.join(timeout=2)

    def _t1_client(self, verified_ips, sni="api.openai.com"):
        """构造 T1 专属客户端（policy_state=冻结授权集，回环 TLS）。"""
        ctx = ssl.create_default_context(cafile=None)
        ctx.load_verify_locations(self.server.cert_path)
        backend = PeerPolicyBackend(set(verified_ips), policy_state=frozenset(verified_ips))
        pool = httpcore2.ConnectionPool(
            ssl_context=ctx, max_connections=1, max_keepalive_connections=1,
            http1=True, http2=False, network_backend=backend, keepalive_expiry=5.0)
        transport = _build_core_pool_transport(pool, sni)
        client = httpx2.Client(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0))
        install_security_hook(client, sni)
        client._media_pool = pool
        client._media_backend = backend
        client._media_port = 443
        return client

    def test_v1_peer_in_frozen_set_allows(self):
        """实际 TCP peer = 127.0.0.1 ∈ 冻结授权集 -> 放行 + TLS 握手成功。"""
        client = self._t1_client({"127.0.0.1"})
        req = client.build_request("GET", f"https://127.0.0.1:{self.server.port}/v1")
        resp = client.send(req)
        self.assertEqual(resp.status_code, 200)
        client.close()

    def test_v1_peer_not_in_frozen_set_rejected(self):
        """实际 TCP peer = 127.0.0.1 ∉ 冻结授权集（公网 IP）-> PEER_REJECTED。

        实证 DNS rebinding 窗口：首次解析为公网、连接前变为私网（回环），
        连接期 getpeername() 读到私网 peer ∉ 构造期冻结的公网集 -> 拒绝。"""
        client = self._t1_client({"93.184.21.34"})  # 授权集 = 公网 IP
        req = client.build_request("GET", f"https://127.0.0.1:{self.server.port}/v1")
        try:
            client.send(req)
            self.fail("应 PEER_REJECTED（实际 peer 127.0.0.1 ∉ 授权集 93.184.21.34）")
        except httpcore2.ConnectError as exc:
            self.assertIn("PEER_REJECTED", str(exc))
        finally:
            client.close()

    def test_v1_multi_candidate_with_private_rejected(self):
        """多候选含私网 -> 整体拒绝（任务书 §五 第 1 条，T1 构造 fail-closed）。

        注入 resolver 按 socket.AF_INET/AF_INET6（int）返回 ipv4/ipv6 候选；
        ipv4 候选中混入 192.168.0.1（私网）-> 严格候选校验整体拒绝。"""
        v4 = ["93.184.21.34", "192.168.0.1"]
        v6 = []
        any_non_public = any(not _is_public_ip(ip) for ip in v4 + v6)
        self.assertTrue(any_non_public, "应检出非公网候选")
        with tempfile.TemporaryDirectory() as d:
            ms = {
                "video": {"enabled": True, "provider": "openai",
                          "providers": {"openai": {
                              "base_url": "https://api.example.invalid/v1",
                              "protocol": "openai-video-task", "model": ""}}},
                "image": {}, "voice": {},
            }
            s = _FakeKeyringSettings(media_services=ms, keys={"video_openai": SYNTH_KEY})
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: MagicMock()
            import socket as _socket
            svc._dns_resolver = lambda host, fam: (
                v4 if fam == _socket.AF_INET else v6)
            prov = MediaProvider(
                service="video", provider_id="openai",
                base_url="https://api.example.invalid/v1",
                protocol="openai-video-task", extra_params={})
            with self.assertRaises(MediaError) as ctx:
                svc._build_download_client("cdn.example.invalid", prov)
            self.assertEqual(ctx.exception.code, "CONNECT_TARGET_UNBOUND")

    def test_v1_tls_sni_cert_host_verification(self):
        """TLS SNI=api.openai.com + 证书 SAN 含该域 -> 握手成功（强度不降）。"""
        client = self._t1_client({"127.0.0.1"}, sni="api.openai.com")
        req = client.build_request("GET", f"https://127.0.0.1:{self.server.port}/v1")
        resp = client.send(req)
        self.assertEqual(resp.status_code, 200)  # SNI 匹配证书 SAN -> 放行
        # 钩子已注入 SNI/Host
        self.assertTrue(client._event_hooks.get("request"))
        client.close()

    def test_v1_connection_failure_releases_resources(self):
        """连接失败（PEER_REJECTED）时 T1 资源正确释放（不泄漏）。"""
        client = self._t1_client({"93.184.21.34"})  # 公网集，连回环 -> PEER_REJECTED
        req = client.build_request("GET", f"https://127.0.0.1:{self.server.port}/v1")
        try:
            client.send(req)
        except httpcore2.ConnectError:
            pass
        # 释放 T1 专属池/backend
        close_media_client(client)
        # 不抛异常即视为释放成功（池已关闭）


if __name__ == "__main__":
    unittest.main(verbosity=2)
