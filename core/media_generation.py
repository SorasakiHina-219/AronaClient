"""
R13-MEDIA-01：多媒体能力核心（视频生成 + 语音合成/识别 + 开放服务商图片生成）

设计（任务 §二/§五/§六）：
  - 服务商配置完全开放：读取 core.settings 的 media_services.{service}，
    用户可添加任意 https:// 服务商；图片/视频/语音配置相互独立。
  - 适配器结构：每个 service 支持按 protocol 路由到不同的解析实现，
    明确记录每个适配器支持的接口与参数（不假设所有服务同一种 JSON）。
  - 复用现有安全连接层（core.image_generation 的 DNS 候选/公网校验/
    PeerPolicyBackend/TLS 钩子桥接），对开放服务商同样 fail-closed。
  - 耗时网络操作全部在调用方后台线程执行（本模块不绑 UI，G9 一致）。
  - 密钥只经 SettingsManager 的 keyring 路径读取，本模块不落盘、
    不进日志、不进异常文本。

已实现并验证的适配器（本地 mock/回环；真实端点为 BLOCKED）：
  image:
    - protocol "openai"（默认）：POST {base_url}/images/generations，
      支持 b64_json 与 url 两种响应形态；url 形态经安全下载。
    - protocol "generic-image"：POST {base_url}{path}，响应取
      data[0].b64_json / data[0].url / image 字段（宽松解析）。
  video:
    - protocol "openai-video-task"：POST {base_url}/videos 创建异步任务
      -> 轮询 GET {base_url}/videos/{task_id} -> 完成后下载 result_url。
      完整处理 task_id / 状态 / 失败 / 超时（任务 §五 最低要求）。
    - protocol "generic-video"：同上的泛化形态，可配 path / poll_path。
  voice:
    - protocol "openai-audio"：POST {base_url}/audio/speech（TTS，
      支持 voice/language/speed/response_format），
      POST {base_url}/audio/transcriptions（STT，上传本地音频）。
"""

from __future__ import annotations

import base64
import json
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpcore2
import httpx2

from core.image_generation import (
    PeerPolicyBackend,
    _build_core_pool_transport,
    _sniff_image_format,
    install_security_hook,
    resolve_dns_candidates,
    validate_candidates,
    save_image_result,
    cleanup_partial,
    ImageGenError,
    IMAGE_CONNECT_TIMEOUT,
    IMAGE_READ_TIMEOUT,
    IMAGE_MAX_OUTPUT_BYTES,
)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VIDEO_RESULTS_DIRNAME = "results"
VIDEO_MAX_OUTPUT_BYTES = 512 * 1024 * 1024   # 视频更大
VIDEO_ALLOWED_EXTS = ("mp4", "webm", "mov", "mkv", "m4v")

VOICE_RESULTS_DIRNAME = "results"
VOICE_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
VOICE_TTS_FORMATS = ("mp3", "opus", "aac", "flac", "wav", "pcm")
VOICE_STT_EXTS = ("mp3", "wav", "ogg", "flac", "m4a", "webm", "mp4", "mpeg")

# 轮询参数
VIDEO_POLL_INTERVAL = 3.0
VIDEO_POLL_TIMEOUT = 300.0

# 视频/语音网络超时（秒）
MEDIA_CONNECT_TIMEOUT = 10.0
MEDIA_READ_TIMEOUT = 300.0        # 视频下载给足时间
VOICE_READ_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# 固定错误分类（G7 风格；用户可见文本无密钥/无 SDK 原文）
# ---------------------------------------------------------------------------


class MediaError(Exception):
    """多媒体能力统一异常（图片沿用 ImageGenError，本类覆盖视频/语音/开放图片）。"""

    def __init__(self, code: str, user_message: str, service: str = "", provider_id: str = ""):
        self.code = code
        self.service = service
        self.provider_id = provider_id
        self.user_message = user_message
        super().__init__(f"{code} [{service}/{provider_id}]: {user_message}")

    @classmethod
    def service_disabled(cls, service, provider_id=""):
        return cls("SERVICE_DISABLED", f"{service} 服务未启用", service, provider_id)

    @classmethod
    def provider_not_configured(cls, service, provider_id=""):
        return cls("PROVIDER_NOT_CONFIGURED", f"{service} 服务商未配置", service, provider_id)

    @classmethod
    def credential_missing(cls, service, provider_id=""):
        return cls("CREDENTIAL_MISSING", "缺少 API Key，请在设置中填写", service, provider_id)

    @classmethod
    def unsupported_protocol(cls, service, protocol=""):
        return cls("UNSUPPORTED_PROTOCOL", f"协议 {protocol or '未指定'} 尚未实现", service)

    @classmethod
    def invalid_base_url(cls, service, provider_id=""):
        return cls("INVALID_BASE_URL", "API 地址需以 https:// 开头", service, provider_id)

    @classmethod
    def connect_target_unbound(cls, service, provider_id=""):
        return cls("CONNECT_TARGET_UNBOUND", "无法验证连接目标，已拒绝", service, provider_id)

    @classmethod
    def dns_resolution_failed(cls, service, provider_id=""):
        return cls("DNS_RESOLUTION_FAILED", "域名解析失败", service, provider_id)

    @classmethod
    def connect_peer_rejected(cls, service, provider_id=""):
        return cls("CONNECT_PEER_REJECTED", "连接目标校验失败，已拒绝", service, provider_id)

    @classmethod
    def tls_verification_failed(cls, service, provider_id=""):
        return cls("TLS_VERIFICATION_FAILED", "TLS 证书验证失败，已拒绝", service, provider_id)

    @classmethod
    def request_timeout(cls, service, provider_id=""):
        return cls("REQUEST_TIMEOUT", "请求超时", service, provider_id)

    @classmethod
    def rate_limited(cls, service, provider_id=""):
        return cls("RATE_LIMITED", "服务商限流，请稍后", service, provider_id)

    @classmethod
    def provider_error(cls, status=0, service="", provider_id=""):
        if isinstance(status, int) and status > 0:
            msg = f"服务商异常（{status}）"
        else:
            msg = "服务商异常"
        return cls("PROVIDER_ERROR", msg, service, provider_id)

    @classmethod
    def task_failed(cls, service, provider_id="", detail=""):
        msg = "任务失败" + (f"：{detail}" if detail else "")
        return cls("TASK_FAILED", msg, service, provider_id)

    @classmethod
    def task_timeout(cls, service, provider_id=""):
        return cls("TASK_TIMEOUT", "任务轮询超时", service, provider_id)

    @classmethod
    def invalid_response(cls, service, provider_id=""):
        return cls("INVALID_RESPONSE", "服务商返回结果无效", service, provider_id)

    @classmethod
    def download_failed(cls, service, provider_id=""):
        return cls("DOWNLOAD_FAILED", "结果下载失败", service, provider_id)

    @classmethod
    def decode_failed(cls, service, provider_id=""):
        return cls("DECODE_FAILED", "结果解码失败，已丢弃", service, provider_id)

    @classmethod
    def output_too_large(cls, service, provider_id=""):
        return cls("OUTPUT_TOO_LARGE", "结果超出大小上限", service, provider_id)

    @classmethod
    def file_not_found(cls, service, provider_id=""):
        return cls("FILE_NOT_FOUND", "源文件不存在或不可读", service, provider_id)

    @classmethod
    def internal_error(cls, service="", provider_id=""):
        return cls("INTERNAL_ERROR", "内部错误", service, provider_id)

    _USER_MESSAGES = {
        "SERVICE_DISABLED": "该服务未启用，请在设置中开启并配置服务商。",
        "PROVIDER_NOT_CONFIGURED": "该服务商未配置，请在设置中添加。",
        "CREDENTIAL_MISSING": "缺少 API Key，请在设置中填写。",
        "UNSUPPORTED_PROTOCOL": "该协议尚未实现。",
        "INVALID_BASE_URL": "API 地址需以 https:// 开头。",
        "CONNECT_TARGET_UNBOUND": "无法验证连接目标，已拒绝。",
        "DNS_RESOLUTION_FAILED": "域名解析失败。",
        "CONNECT_PEER_REJECTED": "连接目标校验失败，已拒绝。",
        "TLS_VERIFICATION_FAILED": "TLS 证书验证失败，已拒绝。",
        "REDIRECT_REJECTED": "检测到重定向，已拒绝。",
        "REQUEST_TIMEOUT": "请求超时。",
        "RATE_LIMITED": "服务商限流，请稍后再试。",
        "PROVIDER_ERROR": "服务商异常。",
        "TASK_FAILED": "任务失败。",
        "TASK_TIMEOUT": "任务轮询超时。",
        "INVALID_RESPONSE": "服务商返回结果无效。",
        "DOWNLOAD_FAILED": "结果下载失败。",
        "DECODE_FAILED": "结果解码失败，已丢弃。",
        "OUTPUT_TOO_LARGE": "结果超出大小上限。",
        "FILE_NOT_FOUND": "源文件不存在或不可读。",
        "INTERNAL_ERROR": "内部错误。",
    }

    @classmethod
    def code_map_get(cls, code, service="", provider_id=""):
        """按错误码取用户安全文案（无密钥/无 SDK 原文）。"""
        return cls._USER_MESSAGES.get(
            code,
            f"内部错误：{code}" if code else "内部错误",
        )

    @classmethod
    def from_exception(cls, exc: BaseException, service: str = "", provider_id: str = ""):
        if isinstance(exc, MediaError):
            return exc
        name = type(exc).__name__
        text = str(exc)
        if "PEER_REJECTED" in text or "ConnectError" in name:
            return cls.connect_peer_rejected(service, provider_id)
        if "TLS" in name or "SSL" in name or "CERTIFICATE" in text.upper():
            return cls.tls_verification_failed(service, provider_id)
        if "Redirect" in name:
            return cls("REDIRECT_REJECTED", "检测到重定向，已拒绝", service, provider_id)
        if "Timeout" in name:
            return cls.request_timeout(service, provider_id)
        if "RateLimit" in name or "429" in text:
            return cls.rate_limited(service, provider_id)
        if "HTTPStatus" in name or "APIStatus" in name:
            status = 0
            resp = getattr(exc, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", 0) or 0
            return cls.provider_error(
                status, service, provider_id
            )
        return cls.internal_error(service, provider_id)


# ---------------------------------------------------------------------------
# 通用安全客户端（复用图片生成的连接绑定层，域名参数化）
# ---------------------------------------------------------------------------


def build_media_secure_client(
    base_url: str,
    dns_resolver=None,
    connect_timeout: float = MEDIA_CONNECT_TIMEOUT,
    read_timeout: float = MEDIA_READ_TIMEOUT,
):
    """按用户配置的 base_url 构造安全 httpx2 客户端（fail-closed）。

    - 解析域名候选 -> 仅保留公网 IP（validate_candidates）；
    - 全私网/无法解析 -> CONNECT_TARGET_UNBOUND / DNS_RESOLUTION_FAILED；
    - TLS 证书 + 主机名校验常开（verify=False 不可达）；
    - 钩子强制 SNI/Host = 域名（与 base_url 主机一致）；
    - trust_env=False / follow_redirects=False。
    """
    m = base_url.split("/https://") if False else None
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MediaError.invalid_base_url("")
    domain = parsed.hostname
    port = parsed.port or 443

    candidates = resolve_dns_candidates(domain, dns_resolver)
    verified = validate_candidates(candidates)

    ctx = ssl.create_default_context()
    backend = PeerPolicyBackend(verified)
    transport = _build_core_pool_transport(backend, domain)
    # 复用 _build_core_pool_transport 需要 pool；这里直接构造。
    pool = httpcore2.ConnectionPool(
        ssl_context=ctx,
        max_connections=4,
        max_keepalive_connections=2,
        http1=True,
        http2=False,
        network_backend=backend,
        keepalive_expiry=5.0,
    )
    transport = _build_core_pool_transport(pool, domain)
    client = httpx2.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx2.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        ),
    )
    install_security_hook(client, domain)
    client._media_pool = pool
    client._media_backend = backend
    client._media_port = port
    return client


def close_media_client(client):
    if client is None:
        return
    pool = getattr(client, "_media_pool", None)
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass
    try:
        client.close()
    except Exception:
        pass


def _media_request_url(base_url: str, path: str) -> str:
    """把相对 path 拼到 base_url 上（保持 https 与主机）。"""
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _httpx_ported_request(
    client, base_url: str, path: str, method: str = "GET",
    json_body=None, headers=None, files=None, data=None,
) -> httpx2.Response:
    """在安全 client 上发一次请求，URL = base_url + path（域名形态，SNI 一致）。

    使用 httpx2 的绝对 URL（https://{host}/{path}），host 取自 base_url，
    与钩子注入的 SNI/Host 保持一致（图片 F1 同款约束）。
    """
    abs_url = _media_request_url(base_url, path)
    hdrs = {
        "Authorization": "",  # 由调用方通过 headers 覆盖；这里占位防漏
    }
    if headers:
        hdrs.update(headers)
    # 去掉空 Authorization（无 key 场景由门禁拦截，不会到这里）
    for k in [k for k, v in hdrs.items() if v == "" and k == "Authorization"]:
        hdrs.pop(k, None)

    kwargs = {"headers": hdrs}
    kwargs["method"] = method
    kwargs["url"] = abs_url
    if json_body is not None:
        kwargs["json"] = json_body
    if files is not None:
        kwargs["files"] = files
        if data is not None:
            kwargs["data"] = data
    elif data is not None:
        kwargs["data"] = data

    try:
        resp = client.request(**kwargs)
    except httpcore2.ConnectError as e:
        raise MediaError.from_exception(e) from None
    except httpx2.HTTPStatusError as e:
        raise MediaError.from_exception(e, service="", provider_id="") from None
    except httpx2.TimeoutException as e:
        raise MediaError.request_timeout("", "") from None
    except httpx2.TLSLError as e:
        raise MediaError.tls_verification_failed("", "") from None
    except httpx2.HTTPError as e:
        raise MediaError.from_exception(e, service="", provider_id="") from None
    return resp


def _safe_raise_for_status(resp, service: str = "", provider_id: str = ""):
    """对已收到的响应做状态检查；非 2xx 统一映射为 MediaError（G7）。

    确保 wait_video_task / synthesize_voice / transcribe_voice / 下载
    等路径中已经拿到响应对象时的 HTTP 错误被正确分类，不泄露密钥。
    """
    try:
        resp.raise_for_status()
    except httpx2.HTTPStatusError as e:
        raise MediaError.from_exception(e, service, provider_id) from None


# ---------------------------------------------------------------------------
# 媒体文件校验与保存（UUID 命名、大小上限、魔数/解码）
# ---------------------------------------------------------------------------


def _sniff_video_ext(data: bytes) -> Optional[str]:
    """尽力从魔数推断视频扩展名（仅用于拒绝对象校验，非完整解码）。"""
    if data[:4] == b"\x00\x00\x00\x18ftyp" or data[:4] == b"\x00\x00\x00\x20ftyp" \
            or data[:4] == b"\x00\x00\x00\x1cftyp" or data[:4] == b"\x00\x00\x01\x00ftyp":
        brand = data[8:12].decode("latin-1", "replace")
        if "qt  " in brand or "isom" in brand or "mp4" in brand or "M4V" in brand or "m4v" in brand:
            return "mp4"
        return "mp4"
    if data[:3] == b"FLV" or data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webm"
    if data[:4] in (b"\x46\x6c\x61\x43",):
        return "flac"
    if data[:2] == b"ID" and data[4:8] in (b"\\x00\\x00\\x00\\x00",):
        return "mov"
    return "mp4"


def save_video_result(data: bytes, results_dir: Path, provider_id: str = "") -> Path:
    if len(data) > VIDEO_MAX_OUTPUT_BYTES:
        raise MediaError.output_too_large("video", provider_id)
    if len(data) < 64:
        raise MediaError.invalid_response("video", provider_id)
    ext = _sniff_video_ext(data)
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"vid_{uuid.uuid4().hex}.{ext}"
    while path.exists():
        path = results_dir / f"vid_{uuid.uuid4().hex}.{ext}"
    path.write_bytes(data)
    return path


def save_voice_result(data: bytes, results_dir: Path, fmt: str, provider_id: str = "") -> Path:
    if len(data) > VOICE_MAX_OUTPUT_BYTES:
        raise MediaError.output_too_large("voice", provider_id)
    if len(data) < 16:
        raise MediaError.invalid_response("voice", provider_id)
    fmt = (fmt or "mp3").lower()
    if fmt not in VOICE_TTS_FORMATS:
        fmt = "mp3"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"voice_{uuid.uuid4().hex}.{fmt}"
    while path.exists():
        path = results_dir / f"voice_{uuid.uuid4().hex}.{fmt}"
    path.write_bytes(data)
    return path


def save_stt_result(text: str, results_dir: Path, provider_id: str = "") -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"stt_{uuid.uuid4().hex}.txt"
    while path.exists():
        path = results_dir / f"stt_{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 服务商配置解析（开放；图片/视频/语音独立）
# ---------------------------------------------------------------------------


class MediaProvider:
    """一次调用所需的服务商快照（配置 + 密钥），与 SettingsManager 解耦。"""

    def __init__(
        self,
        service: str,
        provider_id: str,
        base_url: str,
        model: str = "",
        protocol: str = "",
        api_key: str = "",
        extra_params: Optional[dict] = None,
    ):
        self.service = service
        self.provider_id = provider_id
        self.base_url = base_url
        self.model = model
        self.protocol = protocol or "default"
        self.api_key = api_key
        self.extra_params = dict(extra_params or {})

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def to_safe_desc(self) -> str:
        """可入日志/异常的安全描述（无密钥、无查询串）。"""
        return f"{self.service}/{self.provider_id} {self.host} [{self.protocol}]"


# ---------------------------------------------------------------------------
# 协议适配器：图片
# ---------------------------------------------------------------------------


def _parse_image_payload(resp_json: dict) -> Optional[bytes]:
    """从 OpenAI 图片响应解析首张图（b64_json 优先；url 形态返回 None 让调用方下载）。"""
    data = resp_json.get("data") or []
    if not data or not isinstance(data, list):
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    b64 = first.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception:
            return None
    return None


def _image_url_from_payload(resp_json: dict) -> Optional[str]:
    data = resp_json.get("data") or []
    if data and isinstance(data, list) and isinstance(data[0], dict):
        url = data[0].get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


# ---------------------------------------------------------------------------
# 主服务门面
# ---------------------------------------------------------------------------


class MediaService:
    """多媒体能力门面（图片开放 / 视频 / 语音）。

    用法（G9：调用方置于后台线程）：
        svc = MediaService(settings=SettingsManager(), results_dir=Path("workspace/results"))
        ok, err = svc.is_available("video")
        if not ok: raise MediaError.from_code(err, "video")
        task = svc.start_video(prompt="一只猫")
        path = svc.wait_video_task(task, timeout=300)
        audio = svc.synthesize_voice(text="你好", voice="alloy")
    """

    SUPPORTED_PROTOCOLS = {
        "image": ("openai", "generic-image"),
        "video": ("openai-video-task", "generic-video"),
        "voice": ("openai-audio",),
    }

    def __init__(
        self,
        settings=None,
        results_dir=None,
        dns_resolver=None,
    ):
        self._settings = settings
        self._results_dir = Path(results_dir) if results_dir else Path("workspace") / "results"
        self._dns_resolver = dns_resolver
        self._clients: dict = {}
        self._lock = threading.Lock()

    # -- 门禁 ----------------------------------------------------------

    def is_available(self, service: str, provider_id: str = ""):
        """返回 (ok, error_code_or_None)。"""
        s = self._settings
        if s is None:
            return (False, "SERVICE_DISABLED")
        section = s.get_media_service_section(service)
        if section.get("enabled") is not True:
            return (False, "SERVICE_DISABLED")
        pid = (provider_id or section.get("provider", "")).strip().lower()
        if not pid:
            return (False, "PROVIDER_NOT_CONFIGURED")
        pconf = s.get_media_provider_config(service, pid)
        if pconf is None:
            return (False, "PROVIDER_NOT_CONFIGURED")
        protocol = pconf.get("protocol") or (
            "openai" if service == "image" else
            "openai-video-task" if service == "video" else
            "openai-audio" if service == "voice" else
            "default"
        )
        allowed = self.SUPPORTED_PROTOCOLS.get(service, ("default",))
        if protocol not in allowed:
            return (False, "UNSUPPORTED_PROTOCOL")
        if not s.has_media_api_key(service, pid):
            return (False, "CREDENTIAL_MISSING")
        return (True, None)

    def build_provider(self, service: str, provider_id: str = "") -> MediaProvider:
        s = self._settings
        section = s.get_media_service_section(service)
        pid = (provider_id or section.get("provider", "")).strip().lower()
        pconf = s.get_media_provider_config(service, pid)
        key = s.get_media_api_key(service, pid)
        return MediaProvider(
            service=service,
            provider_id=pid,
            base_url=pconf.get("base_url", ""),
            model=pconf.get("model", ""),
            protocol=pconf.get("protocol") or self.SUPPORTED_PROTOCOLS.get(service, ("default",))[0],
            api_key=key,
            extra_params=pconf.get("extra_params", {}),
        )

    @staticmethod
    def _code_to_error(code: str, service: str = "", provider_id: str = "") -> MediaError:
        m = {
            "SERVICE_DISABLED": MediaError.service_disabled,
            "PROVIDER_NOT_CONFIGURED": MediaError.provider_not_configured,
            "CREDENTIAL_MISSING": MediaError.credential_missing,
            "UNSUPPORTED_PROTOCOL": MediaError.unsupported_protocol,
            "INVALID_BASE_URL": MediaError.invalid_base_url,
            "CONNECT_TARGET_UNBOUND": MediaError.connect_target_unbound,
            "DNS_RESOLUTION_FAILED": MediaError.dns_resolution_failed,
            "CONNECT_PEER_REJECTED": MediaError.connect_peer_rejected,
            "TLS_VERIFICATION_FAILED": MediaError.tls_verification_failed,
            "REQUEST_TIMEOUT": MediaError.request_timeout,
            "RATE_LIMITED": MediaError.rate_limited,
            "PROVIDER_ERROR": MediaError.provider_error,
            "TASK_FAILED": MediaError.task_failed,
            "TASK_TIMEOUT": MediaError.task_timeout,
            "INVALID_RESPONSE": MediaError.invalid_response,
            "DOWNLOAD_FAILED": MediaError.download_failed,
            "DECODE_FAILED": MediaError.decode_failed,
            "OUTPUT_TOO_LARGE": MediaError.output_too_large,
            "FILE_NOT_FOUND": MediaError.file_not_found,
            "INTERNAL_ERROR": MediaError.internal_error,
        }
        fn = m.get(code, MediaError.internal_error)
        return fn(service, provider_id) if code in m else MediaError.internal_error(service, provider_id)

    # -- 安全客户端（按 base_url 缓存；切换域名必须重建 G7）-----------

    def _client_for(self, provider: MediaProvider):
        domain = provider.host
        with self._lock:
            cached = self._clients.get(domain)
            if cached is not None:
                return cached
            client = build_media_secure_client(
                provider.base_url,
                dns_resolver=self._dns_resolver,
            )
            self._clients[domain] = client
            return client

    def close(self):
        with self._lock:
            for c in self._clients.values():
                close_media_client(c)
            self._clients.clear()

    # ------------------------------------------------------------------
    # 图片（开放服务商）
    # ------------------------------------------------------------------

    def generate_image(
        self,
        prompt: str,
        service: str = "image",
        provider_id: str = "",
        size: str = "1024x1024",
        quality: str = "low",
    ) -> Path:
        ok, err = self.is_available(service, provider_id)
        if not ok:
            raise self._code_to_error(err, service, provider_id)
        prov = self.build_provider(service, provider_id)
        if not prov.base_url:
            raise MediaError.invalid_base_url(service, prov.provider_id)
        client = self._client_for(prov)

        payload = {
            "model": prov.model or "gpt-image-1",
            "prompt": prompt,
            "size": prov.extra_params.get("size", size),
            "quality": prov.extra_params.get("quality", quality),
            "n": 1,
        }
        headers = {"Authorization": f"Bearer {prov.api_key}"}
        resp = _httpx_ported_request(
            client, prov.base_url, "/images/generations",
            method="POST", json_body=payload, headers=headers,
        )
        _safe_raise_for_status(resp, service, prov.provider_id)
        resp_json = resp.json()
        data = _parse_image_payload(resp_json)
        if data is None:
            url = _image_url_from_payload(resp_json)
            if url:
                data = self._download(url, client, prov, max_bytes=IMAGE_MAX_OUTPUT_BYTES,
                                      allowed=IMAGE_ALLOWED_EXTS_FALLBACK)
            else:
                raise MediaError.invalid_response(service, prov.provider_id)
        path = save_image_result(data, self._results_dir, prov.provider_id)
        return path

    # ------------------------------------------------------------------
    # 视频（异步任务：task_id / 轮询 / 完成 / 失败 / 超时）
    # ------------------------------------------------------------------

    def start_video(
        self,
        prompt: str,
        service: str = "video",
        provider_id: str = "",
        **params,
    ) -> dict:
        """提交视频生成任务，返回任务句柄 dict（含 task_id / 轮询信息）。

        不阻塞到完成；调用方随后用 wait_video_task 轮询。
        """
        ok, err = self.is_available(service, provider_id)
        if not ok:
            raise self._code_to_error(err, service, provider_id)
        prov = self.build_provider(service, provider_id)
        client = self._client_for(prov)
        headers = {"Authorization": f"Bearer {prov.api_key}"}

        path = prov.extra_params.get("create_path", "/videos")
        payload = {
            "model": prov.model or "sora-2",
            "prompt": prompt,
        }
        payload.update(params)
        payload.update(prov.extra_params.get("create_payload", {}))
        resp = _httpx_ported_request(
            client, prov.base_url, path, method="POST",
            json_body=payload, headers=headers,
        )
        _safe_raise_for_status(resp, service, prov.provider_id)
        body = resp.json()
        task_id = _extract_task_id(body, service, prov.provider_id)
        return {
            "task_id": task_id,
            "service": service,
            "provider_id": prov.provider_id,
            "base_url": prov.base_url,
            "api_key": prov.api_key,
            "poll_path": prov.extra_params.get("poll_path", "/videos/{task_id}"),
            "result_field": prov.extra_params.get("result_field", "url"),
            "status_field": prov.extra_params.get("status_field", "status"),
            "extra": body,
            "client_domain": prov.host,
        }

    def wait_video_task(
        self,
        task: dict,
        timeout: float = VIDEO_POLL_TIMEOUT,
        interval: float = VIDEO_POLL_INTERVAL,
        status: Optional[dict] = None,
    ) -> Path:
        """轮询任务直到完成/失败/超时，完成后下载结果，返回保存路径。

        status（可选 dict）会被实时更新：{"state": "...", "attempts": n}，
        便于 UI 显示进度而不阻塞主线程判断（实际轮询在后台线程）。
        """
        prov_domain = task["client_domain"]
        client = self._client_for_from_handle(task)
        headers = {"Authorization": f"Bearer {task['api_key']}"}
        base_url = task["base_url"]
        poll_fmt = task["poll_path"]
        result_field = task["result_field"]
        status_field = task["status_field"]

        deadline = time.monotonic() + timeout
        attempts = 0
        last_state = "pending"
        while time.monotonic() < deadline:
            poll_path = poll_fmt.replace("{task_id}", task["task_id"])
            resp = _httpx_ported_request(
                client, base_url, poll_path, method="GET", headers=headers,
            )
            _safe_raise_for_status(resp, "video", task["provider_id"])
            body = resp.json()
            state = str(body.get(status_field) or body.get("state") or last_state)
            state = state.lower()
            if status is not None:
                status["state"] = state
                status["attempts"] = attempts
            if state in ("completed", "complete", "succeeded", "success", "done"):
                url = _extract_result_url(body, result_field, state, "video", task["provider_id"])
                data = self._download(url, client, task, max_bytes=VIDEO_MAX_OUTPUT_BYTES,
                                      allowed=VIDEO_ALLOWED_EXTS)
                return save_video_result(data, self._results_dir, task["provider_id"])
            if state in ("failed", "error", "cancelled", "canceled", "timeout"):
                detail = str(body.get("error") or body.get("message") or state)[:200]
                raise MediaError.task_failed("video", task["provider_id"], detail)
            attempts += 1
            time.sleep(interval)
        raise MediaError.task_timeout("video", task["provider_id"])

    def _client_for_from_handle(self, task: dict):
        # 用任务句柄里的 base_url 复用缓存 client
        provider = MediaProvider(
            service=task.get("service", "video"),
            provider_id=task.get("provider_id", ""),
            base_url=task["base_url"],
            api_key=task.get("api_key", ""),
        )
        return self._client_for(provider)

    # ------------------------------------------------------------------
    # 语音（TTS / STT，独立接口；预留对话流程接口）
    # ------------------------------------------------------------------

    def synthesize_voice(
        self,
        text: str,
        service: str = "voice",
        provider_id: str = "",
        voice: str = "alloy",
        response_format: str = "mp3",
        speed: float = 1.0,
        **extra,
    ) -> Path:
        """文本转语音：返回保存的音频文件路径。"""
        ok, err = self.is_available(service, provider_id)
        if not ok:
            raise self._code_to_error(err, service, provider_id)
        prov = self.build_provider(service, provider_id)
        client = self._client_for(prov)
        headers = {"Authorization": f"Bearer {prov.api_key}"}
        payload = {
            "model": prov.model or "gpt-4o-mini-tts",
            "input": text,
            "voice": prov.extra_params.get("voice", voice),
            "response_format": prov.extra_params.get("response_format", response_format),
            "speed": float(prov.extra_params.get("speed", speed)),
        }
        payload.update(extra)
        resp = _httpx_ported_request(
            client, prov.base_url, "/audio/speech",
            method="POST", json_body=payload, headers=headers,
        )
        _safe_raise_for_status(resp, service, prov.provider_id)
        data = resp.content
        fmt = payload.get("response_format", "mp3")
        path = save_voice_result(data, self._results_dir, fmt, prov.provider_id)
        return path

    def transcribe_voice(
        self,
        audio_path,
        service: str = "voice",
        provider_id: str = "",
        language: str = "zh",
        model: str = "",
        **extra,
    ) -> str:
        """语音转文本：读取本地音频文件，返回识别文本（并保存 stt txt）。"""
        ok, err = self.is_available(service, provider_id)
        if not ok:
            raise self._code_to_error(err, service, provider_id)
        prov = self.build_provider(service, provider_id)
        src = Path(audio_path)
        if not src.exists() or not src.is_file():
            raise MediaError.file_not_found(service, prov.provider_id)
        suffix = src.suffix.lstrip(".").lower()
        if suffix and suffix not in VOICE_STT_EXTS:
            raise MediaError.invalid_response(service, prov.provider_id)
        client = self._client_for(prov)
        headers = {"Authorization": f"Bearer {prov.api_key}"}
        audio_bytes = src.read_bytes()
        files = {"file": (src.name, audio_bytes, "audio/" + suffix or "application/octet-stream")}
        data = {
            "model": model or prov.model or "whisper-1",
            "language": language or prov.extra_params.get("language", "zh"),
            "response_format": "text",
        }
        data.update(extra)
        resp = _httpx_ported_request(
            client, prov.base_url, "/audio/transcriptions",
            method="POST", files=files, data=data, headers=headers,
        )
        _safe_raise_for_status(resp, service, prov.provider_id)
        text = resp.text.strip()
        if not text:
            raise MediaError.invalid_response(service, prov.provider_id)
        save_stt_result(text, self._results_dir, prov.provider_id)
        return text

    # 预留：未来把 STT -> 语言模型 -> TTS 连成对话流程的接缝（任务 §六）。
    # 当前不实现实时对话；仅暴露接口签名，避免把未完成能力标记为已完成。
    def voice_conversation(self, transcript: str, **kwargs):
        raise MediaError.unsupported_protocol("voice", "voice-conversation")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _download(
        self,
        url: str,
        client,
        handle_or_prov,
        max_bytes: int,
        allowed,
    ) -> bytes:
        """从服务商返回的 https URL 下载二进制结果（经安全 client）。

        follow_redirects=False：若返回 3xx -> 拒绝（任务 §七）。
        大小上限 + 魔数/扩展名校验。
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise MediaError.download_failed(getattr(handle_or_prov, "service", "video"), getattr(handle_or_prov, "provider_id", ""))
        target_path = parsed.path or "/"
        if parsed.query:
            target_path += "?" + parsed.query
        resp = _httpx_ported_request(
            client, f"https://{parsed.netloc}", target_path, method="GET",
        )
        _safe_raise_for_status(
            resp,
            getattr(handle_or_prov, "service", ""),
            getattr(handle_or_prov, "provider_id", ""),
        )
        data = resp.content
        if len(data) > max_bytes:
            raise MediaError.output_too_large(getattr(handle_or_prov, "service", ""), getattr(handle_or_prov, "provider_id", ""))
        if not data:
            raise MediaError.download_failed(getattr(handle_or_prov, "service", ""), getattr(handle_or_prov, "provider_id", ""))
        return data


def _extract_task_id(body: dict, service: str, provider_id: str) -> str:
    for k in ("id", "task_id", "task"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise MediaError.invalid_response(service, provider_id)


def _extract_result_url(body: dict, result_field: str, state: str, service: str, provider_id: str) -> str:
    v = body.get(result_field)
    if isinstance(v, str) and v.startswith("https://"):
        return v
    # 兼容 {"result": {"url": "..."}} 或 {"results": [{"url":...}]}
    nested = body.get("result")
    if isinstance(nested, dict):
        v = nested.get("url") or nested.get(result_field)
        if isinstance(v, str) and v.startswith("https://"):
            return v
    results = body.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            v = first.get("url") or first.get(result_field)
            if isinstance(v, str) and v.startswith("https://"):
                return v
    raise MediaError.invalid_response(service, provider_id)


# 图片下载扩展名回退（save_image_result 会再做魔数+Pillow 校验）
IMAGE_ALLOWED_EXTS_FALLBACK = ("png", "jpg", "jpeg", "webp")
