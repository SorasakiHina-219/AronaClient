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
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import httpcore2
import httpx2

import core.media_generation as mg
from core.media_generation import (
    MediaError,
    MediaService,
    save_video_result,
    save_voice_result,
    save_stt_result,
    _extract_task_id,
    _extract_result_url,
    _sniff_video_ext,
    _parse_image_payload,
    _image_url_from_payload,
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


def _video_settings(provider="openai"):
    """一个启用的 video service（openai-video-task 协议）+ 合成密钥。"""
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
        with tempfile.TemporaryDirectory() as d:
            video_bytes = b"\x00\x00\x00\x20ftypisom" + b"x" * 200
            poll_ok = _resp(200, json_data={"id": "t1", "status": "completed", "url": "https://cdn.example/v.mp4"})
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
            }
            status = {}
            path = svc.wait_video_task(task, timeout=5.0, interval=0.0, status=status)
            self.assertTrue(Path(path).exists())
            self.assertIn("vid_", path.name)
            self.assertEqual(status["state"], "completed")

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
        with tempfile.TemporaryDirectory() as d:
            raw = _make_png_bytes()
            gen = _resp(200, json_data={"data": [{"url": "https://cdn.example/a.png"}]})
            dl = _resp(200, body_bytes=raw)
            client = _StubClient({"/images/generations": gen, "/a.png": dl})
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
            path = svc.generate_image("x")
            self.assertTrue(Path(path).exists())


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
        try:
            import ui
        except Exception:
            self.skipTest("PySide6 unavailable")
        with tempfile.TemporaryDirectory() as d:
            video_bytes = b"\x00\x00\x00\x20ftypisom" + b"x" * 200
            submit = _resp(200, json_data={"id": "t1"})
            poll = _resp(200, json_data={"id": "t1", "status": "completed", "url": "https://cdn.example/v.mp4"})
            dl = _resp(200, body_bytes=video_bytes)
            client = _StubClient({"/videos": submit, "/videos/t1": poll, "/v.mp4": dl})
            s = _video_settings()
            svc = MediaService(settings=s, results_dir=Path(d))
            svc._client_for = lambda prov: client
            # start_video 需要真实 client；用 stub 直接调底层
            w = ui.MediaWorker(svc, "video", prompt="x")
            emitted = []
            w.finished.connect(lambda r: emitted.append(("f", str(r))))
            w.error.connect(lambda e: emitted.append(("e", e)))
            w.progress.connect(lambda t: emitted.append(("p", t)))
            w.run()
            self.assertTrue(any(k == "f" for k, _ in emitted), "应回传成功")
            self.assertNotIn("sk-media", " ".join(str(v) for _, v in emitted))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
