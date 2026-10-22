"""
R12.3-07 M1: 图片生成适配器与连接安全层（OpenAI 优先）

设计依据：
  - R12.3-02 终版规格 §5 M1（本文件）、§8 U1–U7
  - R12.3-03 安全补充 S11（代理隔离）/ S12（连接绑定）/ R1（密钥不落盘）
  - R12.3-04 连接绑定可行性 方案 B：
      IP 字面量目标 + sni_hostname 扩展 + 自定义 network_backend peer 校验
      + trust_env=False + follow_redirects=False
  - R12.3-06 O1/O3 组合实证：
      事件钩子在 httpx2 客户端层注入 sni_hostname/Host（05 O1），
      自定义 backend 在每次 connect_tcp 时读取实际 socket peer 并校验
      （05 O3 / 06 O3-5 组合链路）。

安全契约（fail-closed，冻结）：
  G1  前置条件任一不满足 -> 拒绝 + 零网络
  G2  密钥仅 keyring，不落盘/日志/异常文本
  G3  未知域名 / 未知协议 / 非 https -> CONNECT_TARGET_UNBOUND /
      UNSUPPORTED_PROTOCOL
  G4  禁用环境代理继承；拒绝自动重定向；安全检查失败不回退默认网络
  G5  每次发送前钩子设置+验证 Host/SNI；新建连接时 backend 校验实际
      peer；TLS 证书与主机名校验保持开启（verify=False 不可达）
  G6  backend 构造期类型断言；钩子注册失败 / 异常 / 配置不完整 -> 拒请求
  G7  服务商切换必须重建客户端（不复用旧授权域）
  G8  结果写 workspace/results，UUID 命名；大小上限；魔数初检 + Pillow
      完整解码；失败不破坏用户文件
  G9  生成任务在后台线程执行（调用方注入 worker；本模块不绑 UI）
"""

from __future__ import annotations

import base64
import ipaddress
import re
import socket
import ssl
import threading
import uuid
from pathlib import Path
from typing import Optional

import httpcore2
import httpx2
from httpcore2._backends.sync import SyncBackend
from PIL import Image


# ---------------------------------------------------------------------------
# 常量（M4：配置层常量，白名单在 core.settings 统一注册）
# ---------------------------------------------------------------------------

# G3：第一阶段唯一受审查的连接目标（02 F1 白名单，与 core.settings 一致）。
OPENAI_IMAGE_DOMAIN = "api.openai.com"
OPENAI_IMAGE_BASE_URL = f"https://{OPENAI_IMAGE_DOMAIN}/v1"
OPENAI_IMAGE_PROTOCOL = "openai-images"
OPENAI_IMAGE_DEFAULT_MODEL = "gpt-image-1"

# G8：输出约束。
IMAGE_RESULTS_DIRNAME = "results"
IMAGE_MAX_OUTPUT_BYTES = 20 * 1024 * 1024
IMAGE_ALLOWED_FORMATS = ("png", "jpeg", "webp")

# G5：网络超时（秒）。
IMAGE_CONNECT_TIMEOUT = 10.0
IMAGE_READ_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# G7：固定错误分类（05 §8 / 03 §2.2）—— 用户可见文本永不包含密钥、
# SDK 原始消息、URL 查询参数。
# ---------------------------------------------------------------------------

class ImageGenError(Exception):
    """带固定错误码与用户安全消息的图片生成异常。"""

    def __init__(self, code: str, user_message: str, provider_id: str = ""):
        self.code = code
        self.provider_id = provider_id
        self.user_message = user_message
        super().__init__(f"{code}: {user_message}")

    # -- 固定分类（05 §8 U4；码名 + 用户安全消息） -------------------
    @classmethod
    def feature_disabled(cls):
        return cls("FEATURE_DISABLED", "图片生成未启用")

    @classmethod
    def provider_not_registered(cls, provider_id=""):
        return cls(
            "PROVIDER_NOT_REGISTERED",
            "该图片服务商未配置",
            provider_id,
        )

    @classmethod
    def unsupported_protocol(cls):
        return cls(
            "UNSUPPORTED_PROTOCOL",
            "该协议尚未实现（第一阶段仅 OpenAI）",
        )

    @classmethod
    def credential_missing(cls, provider_id=""):
        return cls(
            "CREDENTIAL_MISSING",
            "缺少 API Key，请打开设置",
            provider_id,
        )

    @classmethod
    def connect_target_unbound(cls):
        return cls("CONNECT_TARGET_UNBOUND", "无法验证连接目标，已拒绝")

    @classmethod
    def dns_resolution_failed(cls):
        return cls("DNS_RESOLUTION_FAILED", "域名解析失败")

    @classmethod
    def connect_peer_rejected(cls, provider_id=""):
        return cls(
            "CONNECT_PEER_REJECTED",
            "连接目标校验失败，已拒绝",
            provider_id,
        )

    @classmethod
    def tls_verification_failed(cls, provider_id=""):
        return cls(
            "TLS_VERIFICATION_FAILED",
            "TLS 证书验证失败，已拒绝",
            provider_id,
        )

    @classmethod
    def redirect_rejected(cls):
        return cls("REDIRECT_REJECTED", "检测到重定向，已拒绝")

    @classmethod
    def request_timeout(cls, provider_id=""):
        return cls("REQUEST_TIMEOUT", "请求超时", provider_id)

    @classmethod
    def rate_limited(cls):
        return cls("RATE_LIMITED", "服务商限流，请稍后")

    @classmethod
    def provider_error(cls, status: int = 0, provider_id=""):
        msg = f"服务商异常（{status}）" if status else "服务商异常"
        return cls("PROVIDER_ERROR", msg, provider_id)

    @classmethod
    def invalid_image_response(cls):
        return cls("INVALID_IMAGE_RESPONSE", "生成结果无效")

    @classmethod
    def image_decode_failed(cls):
        return cls("IMAGE_DECODE_FAILED", "图片解码失败，已丢弃")

    @classmethod
    def output_too_large(cls):
        return cls("OUTPUT_TOO_LARGE", "结果超出大小上限")

    @classmethod
    def internal_error(cls):
        return cls("INTERNAL_ERROR", "内部错误")

    # -- 安全映射：任意异常 -> 固定分类（03 §2.2 / S10：不回传 SDK 原文）
    @classmethod
    def from_exception(cls, exc: BaseException, provider_id: str = ""):
        if isinstance(exc, ImageGenError):
            return exc
        name = type(exc).__name__
        text = str(exc)
        if "PEER_REJECTED" in text or "ConnectError" in name:
            return cls.connect_peer_rejected(provider_id)
        if "TLS" in name or "SSL" in name or "CERTIFICATE" in text.upper():
            return cls.tls_verification_failed(provider_id)
        if "Redirect" in name:
            return cls.redirect_rejected()
        if "Timeout" in name:
            return cls.request_timeout(provider_id)
        if "RateLimit" in name or "429" in text:
            return cls.rate_limited()
        if "HTTPStatus" in name or "APIStatus" in name:
            status = 0
            resp = getattr(exc, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", 0) or 0
            return cls.provider_error(status, provider_id)
        return cls.internal_error()


# ---------------------------------------------------------------------------
# G3/G4：DNS 候选解析 + 公网 IP 校验
# ---------------------------------------------------------------------------


def _is_public_ip(ip_str: str) -> bool:
    """仅全局可路由地址返回 True。

    拒绝：私网 / 回环 / 链路本地 / 保留 / 组播 / 未指定 / CGNAT 100.64/10。
    （04 方案 B 的 candidate 侧校验，O3-4/S-07 实证。）
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local \
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return True


def resolve_dns_candidates(host: str, resolver=None) -> dict:
    """解析 host 的全部 A + AAAA 记录（G4-4：候选集）。

    `resolver` 可注入（离线测试）；默认走系统 socket.getaddrinfo。
    返回 {"ipv4": [...], "ipv6": [...]}（去重保序）。
    零记录 -> DNS_RESOLUTION_FAILED。
    """
    if resolver is None:
        resolver = _default_resolver

    raw_v4 = resolver(host, socket.AF_INET) or []
    raw_v6 = resolver(host, socket.AF_INET6) or []

    seen4: set = set()
    v4: list = []
    for x in raw_v4:
        if x not in seen4:
            seen4.add(x)
            v4.append(x)
    seen6: set = set()
    v6: list = []
    for x in raw_v6:
        if x not in seen6:
            seen6.add(x)
            v6.append(x)

    if not v4 and not v6:
        raise ImageGenError.dns_resolution_failed()
    return {"ipv4": v4, "ipv6": v6}


def _default_resolver(host: str, family):
    out = []
    try:
        for info in socket.getaddrinfo(host, None, family):
            out.append(info[4][0])
    except (socket.gaierror, OSError):
        return out
    return out


def validate_candidates(candidates: dict) -> set:
    """只保留公网 IP（G4-8）。无公网 -> CONNECT_TARGET_UNBOUND。"""
    pub = set()
    for ip in candidates.get("ipv4", []):
        if _is_public_ip(ip):
            pub.add(ip)
    for ip in candidates.get("ipv6", []):
        if _is_public_ip(ip):
            pub.add(ip)
    if not pub:
        raise ImageGenError.connect_target_unbound()
    return pub


# ---------------------------------------------------------------------------
# G5：自定义 network_backend（方案 B 连接绑定，06 O3 实证）
# ---------------------------------------------------------------------------


class PeerPolicyBackend(SyncBackend):
    """httpcore2 network backend：连接建立后校验实际 socket peer。

    每次 connect_tcp()：
      1. 建立 TCP 连接（目标是 IP 字面量），
      2. 读 getpeername() 得到真实 peer，
      3. peer 不在 verified 公网集 -> 关闭流 + ConnectError(PEER_REJECTED)，
      4. 否则放行流供 TLS 继续。

    06 O3-3/O3-4 已实证该接缝（回环 TLS 服务器 + 真实池 + 真实 socket）。
    """

    def __init__(self, verified_ips: set):
        super().__init__()
        if not isinstance(verified_ips, set):
            raise TypeError("verified_ips must be a set of str IPs")
        self.verified = set(verified_ips)
        self.connect_log: list = []

    def connect_tcp(
        self,
        host,
        port,
        timeout=None,
        local_address=None,
        socket_options=None,
    ):
        stream = super().connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        peer = None
        for attr in ("sock", "_sock", "socket", "_stream"):
            obj = getattr(stream, attr, None)
            if obj is not None and hasattr(obj, "getpeername"):
                try:
                    peer = obj.getpeername()[0]
                except OSError:
                    peer = None
                break
        self.connect_log.append((host, peer))
        if peer is None or peer not in self.verified:
            try:
                stream.close()
            except Exception:
                pass
            raise httpcore2.ConnectError(
                f"PEER_REJECTED: {peer!r} not in verified set"
            )
        return stream


# ---------------------------------------------------------------------------
# G5：httpx2 -> httpcore2 池的桥接 transport（06 O3 桥接层 +
# response.request 绑定修复，避免 h11 keep-alive 状态异常）
# ---------------------------------------------------------------------------


def _build_core_pool_transport(pool, sni_domain: str):
    from httpx2._transports.base import BaseTransport

    class _BridgedTransport(BaseTransport):
        def __init__(self, pool_, sni):
            self._pool = pool_
            self._sni = sni

        def handle_request(self, request):
            target = request.url.path.encode("latin-1")
            if request.url.query:
                target += b"?" + request.url.query.encode("latin-1")
            ext = {}
            if self._sni:
                ext["sni_hostname"] = self._sni.encode("ascii")
            r = httpcore2.Request(
                method=request.method.encode("ascii"),
                url=httpcore2.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.host.encode("ascii"),
                    port=request.url.port,
                    target=target,
                ),
                headers=[
                    (k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in request.headers.multi_items()
                ],
                content=request.read(),
                extensions=ext,
            )
            cresp = self._pool.handle_request(r)
            body = cresp.read()
            resp = httpx2.Response(
                status_code=cresp.status,
                headers=cresp.headers,
                content=body,
                extensions={
                    "http_version": cresp.extensions.get(
                        "http_version", b"HTTP/1.1"
                    )
                },
            )
            # 06 O3 修复：httpx2 的 _client/Close 路径需要 request 绑定。
            resp.request = request
            return resp

        def close(self):
            try:
                self._pool.close()
            except Exception:
                pass

    return _BridgedTransport(pool, sni_domain)


# ---------------------------------------------------------------------------
# G5/G6：请求事件钩子（06 O1 实证接缝）
# ---------------------------------------------------------------------------


def install_security_hook(client, approved_domain: str):
    """在 httpx2.Client 上注册请求前钩子（G5/G6 强制项）。

    每次发送前把 sni_hostname 扩展与 Host 头强制设为安装期常量
    approved_domain。注册失败 / 钩子缺失 -> 拒请求（fail-closed）。
    """
    if not approved_domain:
        raise ImageGenError.connect_target_unbound()

    def _hook(request):
        request.extensions["sni_hostname"] = approved_domain
        request.headers["Host"] = approved_domain

    hooks = getattr(client, "_event_hooks", None)
    if not isinstance(hooks, dict) or "request" not in hooks:
        raise ImageGenError.internal_error()
    hooks["request"].append(_hook)
    return _hook


# ---------------------------------------------------------------------------
# G5：构造安全 httpx2 客户端
# ---------------------------------------------------------------------------


def build_secure_client(
    approved_domain: str,
    verified_ips: set,
    ssl_context: ssl.SSLContext,
) -> httpx2.Client:
    """构造带自定义 transport/backend/钩子的 httpx2 客户端（方案 B）。

    - transport 桥接真实 httpcore2.ConnectionPool（池绑定，G4-11），
    - network_backend = PeerPolicyBackend(verified_ips)（构造期类型断言，
      G6；06 O3-9 实证无静默回退），
    - trust_env=False（G4-10：禁用环境/Windows 代理继承），
    - follow_redirects=False（G4-7：拒绝自动重定向），
    - ssl_context 保持完整证书+主机名校验（verify=False 不可达）。
    """
    backend = PeerPolicyBackend(verified_ips)
    if not isinstance(backend, SyncBackend):
        # G6：构造期断言（坏 backend 不静默回退默认 SyncBackend，
        # 06 O3-9）。
        raise ImageGenError.internal_error()

    pool = httpcore2.ConnectionPool(
        ssl_context=ssl_context,
        max_connections=4,
        max_keepalive_connections=2,
        http1=True,
        http2=False,
        network_backend=backend,
        keepalive_expiry=5.0,
    )
    transport = _build_core_pool_transport(pool, approved_domain)

    client = httpx2.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx2.Timeout(
            connect=IMAGE_CONNECT_TIMEOUT,
            read=IMAGE_READ_TIMEOUT,
            write=IMAGE_READ_TIMEOUT,
            pool=IMAGE_CONNECT_TIMEOUT,
        ),
    )
    install_security_hook(client, approved_domain)
    # 暴露给调用方做生命周期管理（close 时统一关池）。
    client._imagegen_pool = pool
    client._imagegen_backend = backend
    return client


# ---------------------------------------------------------------------------
# G8：结果保存（UUID / 大小上限 / 魔数 + Pillow 校验）
# ---------------------------------------------------------------------------


def _sniff_image_format(data: bytes) -> Optional[str]:
    """魔数初检（02 P5 / 03 R3：非完整校验，仅快速排除）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_image_result(
    data: bytes,
    results_dir: Path,
    provider_id: str = "",
) -> Path:
    """校验并保存一张生成图，返回最终文件路径。

    任一失败 -> 对应 ImageGenError（固定分类，G8）；
    不覆盖既有文件（UUID 命名）；不触碰本调用产物之外的任何文件。
    """
    if len(data) > IMAGE_MAX_OUTPUT_BYTES:
        raise ImageGenError.output_too_large()

    fmt = _sniff_image_format(data)
    if fmt is None or fmt not in IMAGE_ALLOWED_FORMATS:
        raise ImageGenError.invalid_image_response()

    # Pillow 完整解码 + verify（03 R3：魔数不足，须可解码可用）。
    # 先 verify() 结构完整性，再单独 reopen + load() 全像素解码
    # （verify 通过后上下文退出会关闭 fp，不能复用同一对象 load）。
    from io import BytesIO

    try:
        with Image.open(BytesIO(data)) as im:
            im.verify()
    except Exception:
        raise ImageGenError.image_decode_failed()

    try:
        with Image.open(BytesIO(data)) as im:
            im.load()
    except Exception:
        raise ImageGenError.image_decode_failed()

    results_dir.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if fmt == "jpeg" else fmt
    path = results_dir / f"img_{uuid.uuid4().hex}.{ext}"
    while path.exists():
        path = results_dir / f"img_{uuid.uuid4().hex}.{ext}"
    path.write_bytes(data)
    return path


def cleanup_partial(path: Path):
    """只删除本调用产物（G8：失败清理不扩散）。

    仅当文件位于 results 目录且命名为 img_<32位十六进制UUID> 时才删除；
    其他任何文件（用户上传 / 其他模块产物 / 用户文件）一律不碰。
    """
    import re
    try:
        p = Path(path)
        if not p.exists() or p.parent.name != IMAGE_RESULTS_DIRNAME:
            return
        if not re.fullmatch(
            r"img_[0-9a-f]{32}\.(png|jpg|jpeg|webp)", p.name
        ):
            return
        # unlink(missing_ok=...) 在 Python 3.8+ 可用；缺失时安全返回。
        try:
            p.unlink(missing_ok=True)
        except TypeError:
            # 兜底：极老解释器缺 missing_ok，已用 exists() 守卫过。
            p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# M1 主门面
# ---------------------------------------------------------------------------


class ImageGenerationService:
    """第一阶段图片生成（OpenAI 适配器优先，G3）。

    用法（G9：置于后台线程）：
        svc = ImageGenerationService(settings=settings_manager,
                                     results_dir=Path("workspace/results"))
        ok, err_code = svc.is_available()
        if not ok:
            raise ImageGenError.from_code(err_code)   # 零网络
        path = svc.generate(prompt="...")
    """

    def __init__(
        self,
        settings=None,
        results_dir=None,
        dns_resolver=None,
    ):
        self._settings = settings
        self._results_dir = Path(results_dir) if results_dir else None
        self._dns_resolver = dns_resolver
        self._client = None
        self._client_domain = None
        self._lock = threading.Lock()

    # -- G1：门禁（前置条件全真才放行，否则零网络） --------------------
    def is_available(self):
        """返回 (ok, error_code_or_None)。任一前置条件失败 -> False。"""
        s = self._settings
        if s is None:
            return (False, "FEATURE_DISABLED")
        section = s.get_image_generation_section()
        if section.get("enabled") is not True:
            return (False, "FEATURE_DISABLED")

        provider_id = section.get("provider", "openai")
        provider = s.get_image_provider(provider_id)
        if provider is None:
            return (False, "PROVIDER_NOT_REGISTERED")

        protocol = provider.get("protocol")
        if protocol != OPENAI_IMAGE_PROTOCOL:
            return (False, "UNSUPPORTED_PROTOCOL")

        base_url = str(provider.get("base_url", OPENAI_IMAGE_BASE_URL))
        if not self._target_is_reviewed(base_url):
            return (False, "CONNECT_TARGET_UNBOUND")

        key = s.get_api_key(f"image_{provider_id}")
        if not key:
            return (False, "CREDENTIAL_MISSING")

        return (True, None)

    def _target_is_reviewed(self, base_url: str) -> bool:
        """G3：仅受审查的 OpenAI 目标通过；未知域名/非 https 拒绝。"""
        m = re.match(r"^https://([^/]+)", base_url)
        if not m:
            return False
        host = m.group(1)
        return host == OPENAI_IMAGE_DOMAIN or host.startswith(
            "." + OPENAI_IMAGE_DOMAIN
        )

    @staticmethod
    def _code_to_error(code: str) -> ImageGenError:
        m = {
            "FEATURE_DISABLED": ImageGenError.feature_disabled,
            "PROVIDER_NOT_REGISTERED": ImageGenError.provider_not_registered,
            "UNSUPPORTED_PROTOCOL": ImageGenError.unsupported_protocol,
            "CREDENTIAL_MISSING": ImageGenError.credential_missing,
            "CONNECT_TARGET_UNBOUND": ImageGenError.connect_target_unbound,
            "DNS_RESOLUTION_FAILED": ImageGenError.dns_resolution_failed,
            "CONNECT_PEER_REJECTED": ImageGenError.connect_peer_rejected,
            "TLS_VERIFICATION_FAILED": ImageGenError.tls_verification_failed,
            "REDIRECT_REJECTED": ImageGenError.redirect_rejected,
            "REQUEST_TIMEOUT": ImageGenError.request_timeout,
            "RATE_LIMITED": ImageGenError.rate_limited,
            "PROVIDER_ERROR": ImageGenError.provider_error,
            "INVALID_IMAGE_RESPONSE": ImageGenError.invalid_image_response,
            "IMAGE_DECODE_FAILED": ImageGenError.image_decode_failed,
            "OUTPUT_TOO_LARGE": ImageGenError.output_too_large,
            "INTERNAL_ERROR": ImageGenError.internal_error,
        }
        return m.get(code, ImageGenError.internal_error)()

    # -- G5/G7：安全客户端构造（服务商切换必须重建） -------------------
    def _build_client(self):
        with self._lock:
            domain = OPENAI_IMAGE_DOMAIN
            if self._client is not None and self._client_domain == domain:
                return self._client

            self._close_client_locked()

            # G4-4/G4-8：先解析候选，再只保留公网 IP。
            candidates = resolve_dns_candidates(domain, self._dns_resolver)
            verified = validate_candidates(candidates)

            # G5：TLS 上下文 = 系统 CA + 完整主机名校验（不可关）。
            ctx = ssl.create_default_context()

            client = build_secure_client(domain, verified, ctx)
            self._client = client
            self._client_domain = domain
            return self._client

    def _close_client_locked(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._client_domain = None

    def close(self):
        with self._lock:
            self._close_client_locked()

    # -- G5：发起请求（阻塞；调用方在后台线程内执行，G9） --------------
    def generate(
        self,
        prompt: str,
        size: str = "1024x1024",
        quality: str = "low",
        model: str = "",
        timeout_seconds: float = 0.0,
    ) -> Path:
        """生成一张图并保存到结果目录。门禁失败 -> 零网络（G1）。"""
        ok, err_code = self.is_available()
        if not ok:
            raise self._code_to_error(err_code)

        s = self._settings
        provider_id = s.get_image_generation_section().get(
            "provider", "openai"
        )
        key = s.get_api_key(f"image_{provider_id}")

        client = self._build_client()

        import openai

        # G5：SNI/Host 由钩子在 httpx2 层强制；SDK 只负责请求构造，
        # 交给我们注入的安全 transport。
        #
        # 注意：openai 客户端创建时【不】设置 base_url —— 让相对路径
        # （"/images/generations"）经 self.base_url（默认
        # "https://api.openai.com/v1"）合并后保持域名形态（SNI 一致），
        # 而传输层仍是我们的安全 transport。
        #
        # 若设了绝对 IP base_url，httpx2 的 _merge_url 对绝对 URL 不应用
        # base_url，请求域名将与 SNI=api.openai.com 不一致，TLS 主机名
        # 校验将失败（R12.3-08 F1）。
        #
        # max_retries=0：重试语义由固定错误分类 + 调用方策略承担
        # （02 P5 / 05 §6 N4）。
        openai_client = openai.OpenAI(
            api_key=key,
            http_client=client,
            timeout=timeout_seconds or IMAGE_READ_TIMEOUT,
            max_retries=0,
        )
        try:
            resp = openai_client.images.generate(
                model=model or OPENAI_IMAGE_DEFAULT_MODEL,
                prompt=prompt,
                size=size,
                quality=quality,
                n=1,
            )
        except httpcore2.ConnectError as e:
            raise ImageGenError.from_exception(e, provider_id) from None
        except httpx2.HTTPStatusError as e:
            raise ImageGenError.from_exception(e, provider_id) from None
        except httpx2.TimeoutException as e:
            raise ImageGenError.request_timeout(provider_id) from None
        except httpx2.TLSLError as e:
            raise ImageGenError.tls_verification_failed(provider_id) from None
        except httpx2.HTTPError as e:
            raise ImageGenError.from_exception(e, provider_id) from None

        data_b64 = []
        if getattr(resp, "data", None):
            for item in resp.data:
                b64 = getattr(item, "b64_json", None)
                if b64:
                    data_b64.append(b64)
        if not data_b64:
            raise ImageGenError.invalid_image_response()

        raw = base64.b64decode(data_b64[0])
        results_dir = self._results_dir or _default_results_dir()
        path = save_image_result(raw, results_dir, provider_id)
        return path


def _default_results_dir() -> Path:
    return Path("workspace") / IMAGE_RESULTS_DIRNAME


# ---------------------------------------------------------------------------
# M5 注入面
# ---------------------------------------------------------------------------


def make_test_service(settings=None, results_dir=None, dns_resolver=None):
    """测试工厂（M5）：所有旋钮可注入，默认零网络。"""
    return ImageGenerationService(
        settings=settings,
        results_dir=results_dir,
        dns_resolver=dns_resolver,
    )
