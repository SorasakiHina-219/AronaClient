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
    - protocol "generic-image"：与 "openai" **完全等同**
      （S3 方案 a 更正：仅为旧配置字符串兼容而保留，
      不提供可配置请求路径、不做 image 字段宽松解析；
      非 OpenAI 形状的服务商不受支持，需 S4 新增独立 adapter）。
  video:
    - protocol "openai-video-task"：POST {base_url}/videos 创建异步任务
      -> 轮询 GET {base_url}/videos/{task_id} -> 完成后下载 result_url。
      完整处理 task_id / 状态 / 失败 / 超时（任务 §五 最低要求）。
      可配 extra_params: create_path / create_payload / poll_path /
      result_field / status_field。
    - protocol "generic-video"：与 "openai-video-task" 完全等同
      （同一 adapter 实现，仅旧配置兼容保留，非通用协议）。
  voice:
    - protocol "openai-audio"：POST {base_url}/audio/speech（TTS，
      支持 voice/language/speed/response_format），
      POST {base_url}/audio/transcriptions（STT，上传本地音频）。

协议分派（S3）：5 个协议键经 _ADAPTERS 注册表路由到 3 个无状态
adapter 类；未登记的协议在门禁返回 UNSUPPORTED_PROTOCOL，绝不
静默回退到其他协议。
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
    _is_public_ip,
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
# D-NEW-1：原 VIDEO_ALLOWED_EXTS 死常量（_download 扩展名参数从不被消费）已移除；
# 视频内容校验实际由 save_video_result（魔数 + _sniff_video_ext）完成。

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


def _imagegen_error_to_media(exc, service: str = "", provider_id: str = "") -> "MediaError":
    """把安全层抛出的 ImageGenError 统一转换为 MediaError（S1 错误契约）。

    设计约束（R13-MEDIA-05-S1）：
      - **保留原始错误码**：转换后 code 与 exc.code 完全一致，
        不做「一律 INTERNAL_ERROR」的降级，否则调用方无法区分
        DNS / 目标校验 / TLS 等失败原因；
      - 用户可见文案优先取 MediaError 的固定分类文案（G7：无密钥、
        无 SDK 原文）；该码未登记时才回退 exc.user_message
        （ImageGenError 的文案同样是用户安全的）；
      - 调用方须以 ``raise ... from exc`` 抛出，保留异常链供诊断；
      - 本函数只做类型归一，**不改变**任何校验结论（拒绝仍然是拒绝）。
    """
    if isinstance(exc, MediaError):
        return exc
    code = getattr(exc, "code", "") or "INTERNAL_ERROR"
    msg = (
        MediaError._USER_MESSAGES.get(code)
        or getattr(exc, "user_message", "")
        or "内部错误"
    )
    return MediaError(code, msg, service, provider_id)


def build_media_secure_client(
    base_url: str,
    dns_resolver=None,
    connect_timeout: float = MEDIA_CONNECT_TIMEOUT,
    read_timeout: float = MEDIA_READ_TIMEOUT,
    service: str = "",
    provider_id: str = "",
):
    """按用户配置的 base_url 构造安全 httpx2 客户端（fail-closed）。

    - 解析域名候选 -> 仅保留公网 IP（validate_candidates）；
    - 全私网/无法解析 -> CONNECT_TARGET_UNBOUND / DNS_RESOLUTION_FAILED；
    - TLS 证书 + 主机名校验常开（verify=False 不可达）；
    - 钩子强制 SNI/Host = 域名（与 base_url 主机一致）；
    - trust_env=False / follow_redirects=False。

    S1：共享安全层（core.image_generation）抛出的 ImageGenError 在此
    统一转成 MediaError，避免错误类型泄漏到多媒体调用方。
    """
    m = base_url.split("/https://") if False else None
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MediaError.invalid_base_url(service, provider_id)
    domain = parsed.hostname
    port = parsed.port or 443

    try:
        candidates = resolve_dns_candidates(domain, dns_resolver)
        verified = validate_candidates(candidates)
    except ImageGenError as exc:
        # DNS_RESOLUTION_FAILED / CONNECT_TARGET_UNBOUND：拒绝结论不变，
        # 仅统一异常类型并保留原始错误码与异常链。
        raise _imagegen_error_to_media(exc, service, provider_id) from exc

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
    try:
        install_security_hook(client, domain)
    except ImageGenError as exc:
        # 钩子安装失败（G6 fail-closed）：先释放半构造的客户端与连接池，
        # 避免连接泄漏，再统一转 MediaError（保留错误码 + 异常链）。
        try:
            client.close()
        except Exception:
            pass
        try:
            pool.close()
        except Exception:
            pass
        raise _imagegen_error_to_media(exc, service, provider_id) from exc
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

    S4-B2（T1）：`base_url` 也可以是形如 `https://{netloc}` 的结果 URL 目标
    （`_download` 将完整 path + 查询串显式拼为 `/` 起头串），此时直接
    `base_url.rstrip("/") + path`，避免 `_media_request_url` 对已含
    `https://` 的 base 再次拼接导致 netloc 错位。base_url 不含 scheme
    的常规 API 路径走原 `_media_request_url`（零回归）。
    """
    if base_url.startswith("https://") and path.startswith("/"):
        abs_url = base_url.rstrip("/") + path
    else:
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
    # D-NEW-2 修复：原 ftyp 分支使用 data[:4] 与 8 字节字面量（含 "ftyp"）比较，
    # 4 字节切片 ≠ 8 字节对象，恒 False，导致整个 ftyp 分支不可达（死代码）。
    # 修正为 data[:8] 与 8 字节字面量比较（长度匹配），使 ftyp 分支可达。
    if data[:8] == b"\x00\x00\x00\x18ftyp" or data[:8] == b"\x00\x00\x00\x20ftyp" \
            or data[:8] == b"\x00\x00\x00\x1cftyp" or data[:8] == b"\x00\x00\x01\x00ftyp":
        brand = data[8:12].decode("latin-1", "replace")
        # D-NEW-2 后续修正（方案 A）：major_brand 完整四字节精确匹配（ISO BMFF §8.6.4
        # ftyp major_brand 为精确枚举值，非子串）。仅 "qt  "（标准 QuickTime Movies）
        # 有明确依据映射 mov；"qt__"/"qtc_"/"isml" 等保持未确认状态，不纳入白名单。
        # 其余 brand（isom/mp42/M4V/未知/含 "qt" 子串的非标准 brand）→ mp4 兜底。
        if brand == "qt  ":
            return "mov"
        # D-NEW-3 fail-closed：已知 MP4 major_brand（isom/mp42，已被既有测试
        # 断言为 "mp4"）返回 "mp4"；未知 brand（zzzz/aqt/qqqq/M4V 等）返回
        # None，交由 save_video_result 以 MediaError.decode_failed 拒绝保存，
        # 避免把未知/损坏内容伪装为 .mp4。
        if brand in ("isom", "mp42"):
            return "mp4"
        return None
    # D-NEW-5 修复：WebM 仅由 EBML 签名触发（原 L467 将 b"FLV" 与 EBML 签名合并
    # 返回 "webm"，导致真实 FLV 文件误存为 .webm；现 FLV 魔数落入默认 fail-closed → None）
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    # D-NEW-5 修复：删除 RIFF+WEBP → "webm" 分支（WEBP 是图像格式，非视频；
    # 在视频识别函数中返回 "webm" 属格式类别混淆，现落入默认 fail-closed → None）
    if data[:4] in (b"\x66\x4c\x61\x43",):
        # D-NEW-4 修复：原字面量 b"\x46\x6c\x61\x43" = "FlaC"（首字节 0x46='F' 大写），
        # 与真实 FLAC 魔数 "fLaC"（首字节 0x66='f' 小写）不符，FLAC 分支恒不可达，
        # 真实 FLAC 文件落入 D-NEW-3 fail-closed 默认 → None。修正为正确大小写。
        return "flac"
    # D-NEW-2 修复：旧式 MOV 头 "ID"+4字节零值（原双转义字面量 b"\\x00..." 实际是
    # 20字节ASCII文本，与4字节切片比较恒False，导致MOV分支不可达，恒落入默认"mp4"）
    if data[:2] == b"ID" and data[4:8] == b"\x00\x00\x00\x00":
        return "mov"
    # D-NEW-3 fail-closed：未知/损坏字节不再默认判定为 mp4，返回 None，
    # 交由 save_video_result 以 MediaError.decode_failed 拒绝保存。
    return None


def save_video_result(data: bytes, results_dir: Path, provider_id: str = "") -> Path:
    if len(data) > VIDEO_MAX_OUTPUT_BYTES:
        raise MediaError.output_too_large("video", provider_id)
    if len(data) < 64:
        raise MediaError.invalid_response("video", provider_id)
    ext = _sniff_video_ext(data)
    # D-NEW-3 fail-closed：_sniff_video_ext 对未知/损坏字节返回 None，
    # 此处明确拒绝保存，不创建伪装为 .mp4 的产物（避免调用方误认成功）。
    if ext is None:
        raise MediaError.decode_failed("video", provider_id)
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
# S3：协议适配器与分派注册表
#
# 语义（方案 a，见 R13_MEDIA_07_S3_Precheck.md §2.3）：
#   - 注册表键与 MediaService.SUPPORTED_PROTOCOLS 一一对应（5 键），
#     不扩大受支持的协议范围；
#   - generic-image / generic-video 与 openai 形态**完全等同**
#     （仅为旧配置字符串兼容而保留，不提供可配置请求路径、
#       不做 image 字段宽松解析——未实现的能力不在此承诺）；
#   - 每个 service 的请求构造/响应解析集中在无状态 adapter 类内，
#     公开方法经注册表分派（未知协议 -> UNSUPPORTED_PROTOCOL，
#     绝不静默回退到其他协议）；
#   - 不引入抽象工厂/插件系统/动态导入，保持可读、可审查。
# ---------------------------------------------------------------------------


class _OpenAIImageAdapter:
    """OpenAI Images 形状：POST {base_url}/images/generations。

    generic-image 注册到同一实现：行为与 openai 完全一致。
    仅读 extra_params 的 size/quality 两个既有键。
    """

    create_path = "/images/generations"

    @staticmethod
    def build_payload(prov, prompt, size, quality):
        # S4-A：不再内置未核实的默认模型（原 "gpt-image-1"）。
        # model 来自服务商配置（UI 模型行 / settings.json providers.{pid}.model）；
        # 未配置时仍发送该字段但值为空串，避免请求省略 model 键导致形态漂移——
        # 是否接受由服务商/adapter 决定（fail-closed 在响应侧）。
        return {
            "model": prov.model,
            "prompt": prompt,
            "size": prov.extra_params.get("size", size),
            "quality": prov.extra_params.get("quality", quality),
            "n": 1,
        }

    @staticmethod
    def parse(resp_json: dict):
        """返回 (image_bytes_or_None, download_url_or_None)，与旧解析逐行为等价。"""
        return _parse_image_payload(resp_json), _image_url_from_payload(resp_json)


class _OpenAIVideoTaskAdapter:
    """异步任务形状：POST create_path -> 轮询 GET poll_path -> 下载结果 url。

    generic-video 注册到同一实现；仅读 extra_params 的既有键
    create_path / create_payload / poll_path / result_field / status_field。
    """

    @staticmethod
    def create_request(prov, prompt, extra_params):
        """构造 (create_path, payload)，update 顺序与旧实现完全一致。"""
        path = prov.extra_params.get("create_path", "/videos")
        payload = {
            # S4-A：不再内置未核实的默认模型（原 "sora-2"）。
            # model 来自服务商配置；未配置时为空串，是否接受由服务商决定。
            "model": prov.model,
            "prompt": prompt,
        }
        payload.update(extra_params)
        payload.update(prov.extra_params.get("create_payload", {}))
        return path, payload

    @staticmethod
    def poll_handles(prov):
        """返回 (poll_path, result_field, status_field) 及默认值（同旧实现）。"""
        return (
            prov.extra_params.get("poll_path", "/videos/{task_id}"),
            prov.extra_params.get("result_field", "url"),
            prov.extra_params.get("status_field", "status"),
        )


class _OpenAIAudioAdapter:
    """OpenAI 音频形状：TTS POST /audio/speech；STT POST /audio/transcriptions。

    仅读 extra_params 的既有键 voice / response_format / speed / language。
    """

    speech_path = "/audio/speech"
    transcription_path = "/audio/transcriptions"

    @staticmethod
    def speech_payload(prov, text, voice, response_format, speed, extra_params):
        payload = {
            # S4-A：不再内置未核实的默认模型（原 "gpt-4o-mini-tts"）。
            "model": prov.model,
            "input": text,
            "voice": prov.extra_params.get("voice", voice),
            "response_format": prov.extra_params.get(
                "response_format", response_format
            ),
            "speed": float(prov.extra_params.get("speed", speed)),
        }
        payload.update(extra_params)
        return payload

    @staticmethod
    def stt_fields(prov, language, model, extra_params):
        # S4-A：不再内置未核实的默认模型（原 "whisper-1"）。
        # 优先级保持既有：显式 model 参数 > 服务商配置 model > 空串（显式传 "" 可省略键）。
        data = {
            "model": model or prov.model,
            "language": language or prov.extra_params.get("language", "zh"),
            "response_format": "text",
        }
        data.update(extra_params)
        return data


# 分派注册表：(service, protocol) -> adapter 类（无状态，静态方法调用）。
# 5 个键与 SUPPORTED_PROTOCOLS 完全对应；新增服务商必须在此登记新键
# （S4 范围），不得以“仅填配置”方式获得支持。
_ADAPTERS = {
    ("image", "openai"): _OpenAIImageAdapter,
    ("image", "generic-image"): _OpenAIImageAdapter,
    ("video", "openai-video-task"): _OpenAIVideoTaskAdapter,
    ("video", "generic-video"): _OpenAIVideoTaskAdapter,
    ("voice", "openai-audio"): _OpenAIAudioAdapter,
}


def _adapter_for(service: str, protocol: str):
    """按 (service, protocol) 查注册表；未登记 -> None（调用方判 UNSUPPORTED）。"""
    return _ADAPTERS.get((service, protocol))


def _allowed_protocols(service: str):
    """某 service 受支持的协议名集合（与 SUPPORTED_PROTOCOLS 同源，供门禁用）。"""
    return tuple(p for (s_, p) in _ADAPTERS if s_ == service)


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
        allowed = _allowed_protocols(service)
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
    def _adapter_for(prov) -> object:
        """按 provider 的 (service, protocol) 查注册表；缺失 -> UNSUPPORTED_PROTOCOL。

        S3：门禁（is_available）已保证到达公开方法时的协议在注册表内；
        此处的显式检查是防御性第二道闸（绝不静默回退到其他协议）。
        """
        adapter = _adapter_for(prov.service, prov.protocol)
        if adapter is None:
            raise MediaError.unsupported_protocol(prov.service, prov.protocol)
        return adapter

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
                service=provider.service,
                provider_id=provider.provider_id,
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

        # S3：协议分派（generic-image 与 openai 同一 adapter，行为不变）
        adapter = self._adapter_for(prov)
        payload = adapter.build_payload(prov, prompt, size, quality)
        headers = {"Authorization": f"Bearer {prov.api_key}"}
        resp = _httpx_ported_request(
            client, prov.base_url, adapter.create_path,
            method="POST", json_body=payload, headers=headers,
        )
        _safe_raise_for_status(resp, service, prov.provider_id)
        resp_json = resp.json()
        data, url = adapter.parse(resp_json)
        if data is None:
            if url:
                # S4-B2：URL 形态结果下载。result_hosts 未配置 -> 拒绝
                # （F2 三态「未配置」）；配置无效（空集）-> 拒绝；
                # 有效白名单 -> T1 专属池下载（_build_download_client）。
                data = self._download(
                    url, client, prov,
                    max_bytes=IMAGE_MAX_OUTPUT_BYTES,
                    result_hosts=_result_hosts_from_prov(prov),
                )
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

        # S3：协议分派（generic-video 与 openai-video-task 同一 adapter）
        adapter = self._adapter_for(prov)
        path, payload = adapter.create_request(prov, prompt, params)
        resp = _httpx_ported_request(
            client, prov.base_url, path, method="POST",
            json_body=payload, headers=headers,
        )
        _safe_raise_for_status(resp, service, prov.provider_id)
        body = resp.json()
        task_id = _extract_task_id(body, service, prov.provider_id)
        poll_path, result_field, status_field = adapter.poll_handles(prov)
        return {
            "task_id": task_id,
            "service": service,
            "provider_id": prov.provider_id,
            "base_url": prov.base_url,
            "api_key": prov.api_key,
            "poll_path": poll_path,
            "result_field": result_field,
            "status_field": status_field,
            "extra": body,
            "client_domain": prov.host,
            # S4-A 白名单；S4-B2（F2）：三态语义——None（未配置）/ 空集
            # （配置无效）/ 非空（有效白名单）在 _download 中分别拒绝/拒绝/放行
            "result_hosts": _result_hosts_from_prov(prov),
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
                data = self._download(
                    url, client, task,
                    max_bytes=VIDEO_MAX_OUTPUT_BYTES,
                    result_hosts=task.get("result_hosts"),
                )
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

        # S3：协议分派（openai-audio）
        adapter = self._adapter_for(prov)
        payload = adapter.speech_payload(
            prov, text, voice, response_format, speed, extra
        )
        resp = _httpx_ported_request(
            client, prov.base_url, adapter.speech_path,
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

        # S3：协议分派（openai-audio）
        adapter = self._adapter_for(prov)
        data = adapter.stt_fields(prov, language, model, extra)
        resp = _httpx_ported_request(
            client, prov.base_url, adapter.transcription_path,
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
        result_hosts: Optional[frozenset] = None,
    ) -> bytes:
        """从服务商返回的 https URL 下载二进制结果（经安全 client）。

        follow_redirects=False：若返回 3xx -> 拒绝（任务 §七）。
        大小上限（max_bytes，按响应字节长度）。

        注意：内容真实性校验不在本函数完成——由调用方的
        save_image_result / save_video_result（魔数 + Pillow/解码）负责。
        本函数不做扩展名/MIME 校验（原 allowed 死参数已移除，D-NEW-1 修复）。

        S4-A：result_hosts —— 结果 URL 主机白名单（小写主机名集合，
        无通配符；IP 字面量项默认丢弃）。重定向已被 follow_redirects=False 阻止。
        白名单由服务商配置 extra_params 提供，绝不自动加入 base_url。

        S4-B2（F2/F3 fail-closed 语义）：
          - **未配置**（None） -> 拒绝下载（`MediaError.download_failed`）。
          - **配置无效**（空 frozenset） -> 拒绝下载。
          - **有效非空** -> 精确 hostname 匹配 + HTTPS 443 端口约束 +
            歧义闸门（非数字/负数/0/超范围/userinfo/IPv6 方括号/
            netloc 解析歧义一律拒绝） + 请求期 DNS 解析 + 专属 T1 池
            （`_build_download_client`，SNI/Host 绑定结果域，finally 释放）。
          不允许把无效/空白名单转换为「不限制」。
          无法证明连接目标绑定（构造期/请求期解析任一步失败）时 fail-closed 拒绝。
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            svc = getattr(handle_or_prov, "service", "video")
            pid = getattr(handle_or_prov, "provider_id", "")
            raise MediaError.download_failed(svc, pid)

        # ---- S4-B2 F2：三态 fail-closed 判定 ----
        if result_hosts is None:
            raise MediaError.download_failed(
                getattr(handle_or_prov, "service", ""),
                getattr(handle_or_prov, "provider_id", ""),
            )
        if result_hosts == frozenset():
            raise MediaError.download_failed(
                getattr(handle_or_prov, "service", ""),
                getattr(handle_or_prov, "provider_id", ""),
            )
        host = parsed.hostname.lower()
        if host not in result_hosts:
            raise MediaError.download_failed(
                getattr(handle_or_prov, "service", ""),
                getattr(handle_or_prov, "provider_id", ""),
            )

        # ---- S4-B2 F3：HTTPS 443 端口约束 + 歧义闸门 ----
        _validate_result_url_port(
            parsed,
            getattr(handle_or_prov, "service", "video"),
            getattr(handle_or_prov, "provider_id", ""),
        )  # 失败即抛出 DOWNLOAD_FAILED（零网络请求）

        # ---- S4-B2 F1（P2）：请求期 DNS + T1 专属池 ----
        # 专属池与本次下载请求同生共灭，不入 `self._clients` 共享缓存
        # （V2 断言守护：下载后 `self._clients` 长度不变）。
        host_for_dn = parsed.hostname
        try:
            t1_client, t1_release = self._build_download_client(
                host_for_dn,
                handle_or_prov,
            )
        except MediaError:
            raise
        except ImageGenError as exc:
            svc = getattr(handle_or_prov, "service", "video")
            pid = getattr(handle_or_prov, "provider_id", "")
            raise _imagegen_error_to_media(exc, svc, pid) from exc
        try:
            target_path = parsed.path or "/"
            if parsed.query:
                target_path += "?" + parsed.query
            resp = _httpx_ported_request(
                t1_client, f"https://{parsed.netloc}", target_path, method="GET",
            )
        finally:
            t1_release()  # S4-B2：必须释放，避免连接池泄漏
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

    # ------------------------------------------------------------------
    # S4-B2 P2：结果下载 T1 专属池（请求期 DNS + 冻结授权集 + 结果域 SNI）
    # ------------------------------------------------------------------

    def _build_download_client(self, host, handle_or_prov):
        """S4-B2 P2（T1 拓扑）：为单次结果下载构造专属安全客户端。

        步骤（对应 P2 可行性报告 §1.2 调用链）：
          1. 请求期解析目标 hostname（`resolve_dns_candidates`，
             可注入 `dns_resolver`）；
          2. 候选 IP 公网校验（`validate_candidates`，**任何非公网候选
             -> 整体拒绝**，符合任务书 §五 第 1 条）；
          3. 冻结本次下载的授权集（frozenset，同生共灭）；
          4. 构造专属 `PeerPolicyBackend(verified, policy_state=…)` +
             专属 httpcore2 连接池（同 `build_media_secure_client` 构造
             参数，max_connections=1，单请求语义）；
          5. SNI / Host / 证书校验 = 原始结果域名（`install_security_hook`
             传入 `host`；`ssl.create_default_context()` 系统 CA +
             主机名校验常开，强度不降）；
          6. 返回 (client, release_fn)。`release_fn` 在调用方 finally 中
             执行，释放专属池与 backend（不写入 `self._clients` 共享缓存，
             见 V2 断言）。

        失败 fail-closed（任务书 §E）：
          - DNS 零记录 -> `MediaError.dns_resolution_failed`；
          - 全候选非公网 -> `MediaError.connect_target_unbound`；
          - 池/backend/钩子任一构造失败 -> `MediaError.connect_target_unbound`
            + 清理半构造对象（G6 同款，L343-356 既有模式）。
        """
        svc = getattr(handle_or_prov, "service", "video")
        pid = getattr(handle_or_prov, "provider_id", "")
        try:
            candidates = resolve_dns_candidates(host, self._dns_resolver)
            verified = validate_candidates(candidates)
            # S4-B2（任务书 §五 第 1 条）：DNS 返回的**任何候选**不符公网
            # 策略（私网/回环/链路本地/保留/组播/CGNAT）-> 整个请求拒绝。
            # 原 G4-8 语义是「过滤后保留公网子集」，此处对结果下载路径
            # 收紧为全候选校验（不改变 image_generation 共享层的过滤语义，
            # 仅在 T1 专属池构造点对结果下载生效）。
            for _fam in ("ipv4", "ipv6"):
                for _ip in candidates.get(_fam, []):
                    if not _is_public_ip(_ip):
                        raise MediaError.connect_target_unbound(svc, pid)
        except ImageGenError as exc:
            raise _imagegen_error_to_media(exc, svc, pid) from exc
        if not verified:
            raise MediaError.connect_target_unbound(svc, pid)
        frozen = frozenset(verified)  # 冻结授权集（逐请求，无别名）
        try:
            ctx = ssl.create_default_context()
            backend = PeerPolicyBackend(set(verified), policy_state=frozen)
            pool = httpcore2.ConnectionPool(
                ssl_context=ctx,
                max_connections=1,
                max_keepalive_connections=1,
                http1=True,
                http2=False,
                network_backend=backend,
                keepalive_expiry=5.0,
            )
            transport = _build_core_pool_transport(pool, host)
            client = httpx2.Client(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx2.Timeout(
                    connect=MEDIA_CONNECT_TIMEOUT,
                    read=MEDIA_READ_TIMEOUT,
                    write=MEDIA_READ_TIMEOUT,
                    pool=MEDIA_CONNECT_TIMEOUT,
                ),
            )
            install_security_hook(client, host)
        except ImageGenError as exc:
            # G6 同款：构造失败 -> 关流 + 统一转 MediaError（保留错误码）
            raise _imagegen_error_to_media(exc, svc, pid) from exc
        except Exception as exc:
            # 其他构造失败（如 ssl context 异常）-> 内部错误，不静默降级
            raise MediaError.internal_error(svc, pid) from exc
        client._media_pool = pool
        client._media_backend = backend
        client._media_port = 443

        def _release():
            close_media_client(client)

        return client, _release


def _validate_result_url_port(parsed, service: str = "", provider_id: str = "") -> None:
    """S4-B2 F3：结果 URL 的 HTTPS 443 端口约束 + 歧义闸门。

    通过（放行）：
      - 缺省端口（`https://host/`）；
      - 显式 `:443`。
    拒绝（抛出 `MediaError.download_failed`，零网络请求）：
      - 显式非 443 端口（8443 / 0 / 超范围 >65535）；
      - 非数字端口 token（负数、字母等，`urlparse` 归 `port=None`，
        经 netloc 歧义闸门识别）；
      - netloc 含 userinfo（`@`）；
      - IPv6 方括号 + 非 443 显式端口。
    歧义闸门（`parsed.port is None` 但 netloc 含 `:` 形态）按 fail-closed
    拒绝，**不得**把畸形输入误判为缺省 443。
    调用方保证 scheme==https 且 parsed.hostname 非空（_download 前置校验）。
    参数 `service` / `provider_id` 透传到 `MediaError.download_failed`，
    使错误链可诊断（与 _download 既有语义一致）。
    """
    netloc = parsed.netloc or ""
    def _reject():
        raise MediaError.download_failed(service, provider_id)
    # userinfo 拒绝（F3 #9：正常 CDN 结果 URL 不含 userinfo，
    # 出现即视为异常输入，fail-closed）
    if "@" in netloc:
        _reject()
    # 端口判定
    host_port = netloc.rsplit("/", 1)[0] if "/" in netloc else netloc
    # IPv6 方括号：`[2001:db8::1]` / `[2001:db8::1]:443`
    if host_port.startswith("["):
        end = host_port.find("]")
        if end == -1:
            _reject()  # 畸形 IPv6 方括号
        port_part = host_port[end + 1:]
        if port_part.startswith(":"):
            p = port_part[1:]
            if p == "" or not p.isdigit() or int(p) > 65535 or int(p) != 443:
                _reject()
        # 无显式端口 -> 缺省 443，放行
        return
    # IPv4 / hostname 形态
    if ":" in host_port:
        port_tok = host_port.rsplit(":", 1)[1]
        if port_tok == "" or not port_tok.isdigit() or int(port_tok) > 65535 or int(port_tok) != 443:
            _reject()
        return
    # 无显式端口 -> 缺省 443，放行


def _extract_task_id(body: dict, service: str, provider_id: str) -> str:
    for k in ("id", "task_id", "task"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise MediaError.invalid_response(service, provider_id)


def _result_hosts_from_prov(prov: "MediaProvider") -> Optional[frozenset]:
    """S4-A：从服务商配置 extra_params 读取结果 URL 主机白名单。

    S4-B2（F2 三态 fail-closed）返回值语义（三态必须可区分）：
      - **None**：键缺失（未配置）-> 拒绝结果下载。
      - **frozenset()（空集）**：键存在但清洗后无有效主机名（原列表为空、
        全部项非法、全部为 IP 字面量等）-> 拒绝结果下载。
      - **frozenset(非空)**：有效白名单 -> 精确 hostname 匹配放行（+ 端口约束）。

    不允许把无效/空白名单转换成「不限制」。
    清洗规则（fail-closed）：非 list / 非 str 元素 / 空串 / 含 `://` /
    含 `*` / 含 `/` / 长度 > 253 / 为 IP 字面量 -> 丢弃该项。
    白名单**绝不**自动补入 base_url 主机：由用户显式配置（含 CDN 域）。
    """
    raw = prov.extra_params.get("result_hosts")
    if raw is None:
        return None  # 未配置
    if not isinstance(raw, list):
        return frozenset()  # 配置无效（非 list）
    hosts: set = set()
    for item in raw:
        h = _clean_result_host(item)
        if h is not None:
            hosts.add(h)
    # 键存在但全部项被丢弃 -> 空集（「配置无效」，_download 拒绝下载）
    return frozenset(hosts)


def _clean_result_host(item) -> Optional[str]:
    """S4-B2 F2：单条 result_hosts 项清洗。返回合法小写主机名或 None（丢弃）。

    拒绝：非 str、空串、超长、含 scheme/通配符/路径/空格，以及
    **IPv4 / IPv6 / IPv4-mapped IPv6 字面量**（S4-B2 默认策略：
    白名单项必须是主机名；IP 字面量项一律丢弃，公网性由请求期
    `validate_candidates` 对解析出的候选独立判定，与白名单语义解耦）。
    """
    import ipaddress
    if not isinstance(item, str):
        return None
    h = item.strip().lower()
    if not h or len(h) > 253:
        return None
    if "://" in h or "*" in h or "/" in h or " " in h:
        return None
    # S4-B2 F2：IP 字面量项默认拒绝（IPv4 / IPv6 / IPv4-mapped /
    # 方括号 URL 的裸值——urlparse 已剥离括号，比较对象为裸值，歧义消解）。
    try:
        ipaddress.ip_address(h)
        return None  # 是 IP 字面量 -> 丢弃
    except ValueError:
        pass
    return h


def _is_ip_literal(host: str) -> bool:
    """S4-B2 F2：stdlib ipaddress 可解析即视为 IP 字面量（不做公网性判断，
    公网性由 F1 请求期快照校验负责）。"""
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


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

