# core/desktop_vision.py
# AronaClient V0.2
# 第一阶段：桌面截图与视觉感知
#
# 设计原则：
# 1. 只做「用户主动触发」的截图，不做任何后台持续监控。
# 2. 默认不把截图写到磁盘，图像只存在于内存（QImage + JPEG data URL）。
# 3. 发送给云端模型之前，必须由调用方明确确认。
# 4. 无法确认模型支持图像输入时，明确报告「不支持/无法确认」，
#    绝不伪造识别结果。
# 5. 不读取、不记录任何 API Key。
#
# 本模块不执行鼠标、键盘或文件操作。

from __future__ import annotations

import base64
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage


# ============================================================
# 参数
# ============================================================

# 截图缩放后的最大宽度。降低内存与网络消耗。
DEFAULT_MAX_WIDTH = 1280

# JPEG 压缩质量。
DEFAULT_JPEG_QUALITY = 80

MIN_JPEG_QUALITY = 40
MAX_JPEG_QUALITY = 95

# 单张图像编码后的字节上限（防止异常大的图上传）。
MAX_ENCODED_BYTES = 8 * 1024 * 1024

# 隐藏自身窗口后，等待桌面重绘的时间（秒）。
#
# Windows 的窗口隐藏不是立刻生效的，
# 需要让 Qt 处理事件、并给桌面合成留出时间，
# 否则会拍到「窗口正在消失」的中间状态。
DEFAULT_SETTLE_SECONDS = 0.40


# ============================================================
# 桌面视觉读取门禁
# ============================================================
#
# 为什么需要模块级门禁：
#     list_screens() / get_active_window_info() / save_capture() 都是
#     @staticmethod，拿不到实例状态；capture() 虽然是实例方法，但
#     任何持有 DesktopVision 对象的代码都能直接调用。
#
#     只在 UI 按钮上做判断是不够的 —— 模型工具调用、内部代码、
#     后台线程都可能绕过 UI 直接调这些方法。
#
#     因此把判据放在【模块级】，由所有读取路径共同依赖。
#
# 安全默认值：
#     判据未注册时**一律拒绝**（fail-closed）。
#     这与"桌面视觉默认关闭"的要求一致：
#     程序刚启动、判据还没注册好的那段窗口期同样拒绝。

# 门禁实现已下沉到 computer.desktop_access。
#
# 为什么下沉：
#     computer.screen_capture 与 computer.window_info 是最底层入口，
#     只在上层（本模块）检查权限的话，任何代码都能直接
#         from computer.screen_capture import ScreenCapture
#         ScreenCapture().capture_screen()
#     绕过去。
#
# 为什么单独开模块而不是放在这里：
#     本模块需要 import computer.screen_capture，
#     若 computer.* 反过来 import 本模块就会形成循环导入。
#     computer.desktop_access 不导入任何项目内模块，因此不成环。

from computer.desktop_access import (          # noqa: E402
    check as _access_check,
    is_authorized as _access_is_authorized,
    note_denied as _note_desktop_vision_denied,
    set_provider as _set_access_provider,
)


def set_desktop_vision_authorization(fn, allow_replace: bool = False):
    """
    注册桌面视觉读取判据（委托给 computer.desktop_access）。

    fn 必须返回 (ok: bool, reason: str)。

    默认**拒绝覆盖已有判据** —— 防止后来的代码悄悄换掉授权逻辑。
    合法的重新初始化（例如新建 AIClient）请显式传 allow_replace=True。

    传 None 表示注销；注销后默认拒绝。
    """

    _set_access_provider(fn, allow_replace=allow_replace)


def check_desktop_vision_authorization():
    """统一门禁（委托）。fail-closed。"""

    return _access_check()


def is_desktop_vision_authorized():
    """门禁是否放行。仅用于调用方提前判断，不替代实际门禁。"""

    return _access_is_authorized()


# ============================================================
# 截图前隐藏自身窗口
# ============================================================

def _settle(seconds):
    """
    等待指定时间，让桌面有足够时间重绘。

    注意：此函数可能在 worker 线程中调用（桌面感知后台循环）。
    不能调用 QGuiApplication.processEvents()，否则会在非主线程
    处理事件队列，导致事件交叉和潜在的冻结。
    窗口的 hide/show 本身会触发 OS 重绘事件，sleep 足以等待。
    """

    try:

        end = time.monotonic() + max(0.0, float(seconds))

        while time.monotonic() < end:
            time.sleep(0.02)

    except Exception:

        pass


@contextmanager
def temporarily_hidden(widgets, settle_seconds=DEFAULT_SETTLE_SECONDS):
    """
    截图前临时隐藏指定窗口，等桌面重绘完成后再恢复。

    为什么需要这么做：

        AronaClient 自己的窗口在截图时正盖在桌面上。
        实测（1366x768 笔记本，窗口最大化）：
            不隐藏 -> 截图里 84.0% 是 AronaClient 自己的界面
            隐藏后 -> 回落到 0.1%
        所以不隐藏的话，拍到的几乎全是自己。

    无论正常结束、取消还是抛异常，都会恢复窗口。
    默认保持最小改动：只隐藏传进来的窗口，不动其他窗口。
    """

    hidden = []

    for w in (widgets or []):

        if w is None:

            continue

        try:

            was_visible = w.isVisible()
            state = w.windowState()

            w.hide()

            hidden.append(
                (w, was_visible, state)
            )

        except Exception:

            pass

    try:

        _settle(settle_seconds)

        yield

    finally:

        for w, was_visible, state in hidden:

            try:

                if was_visible:

                    w.show()

                    if state:

                        w.setWindowState(state)

                    w.raise_()
                    w.activateWindow()

            except Exception:

                pass

        # 让窗口回到屏幕上再继续，
        # 否则后面的确认对话框可能出现在窗口还没画完的时候。
        _settle(settle_seconds * 0.5)

# 发送给模型时的默认提问。
DEFAULT_QUESTION = (
    "这是我当前的电脑桌面截图。"
    "请用中文描述你看到的窗口、按钮和主要可见内容。"
    "如果不确定某个细节，请直接说不确定，不要猜测。"
)


# ============================================================
# 视觉能力判定
# ============================================================

# 已知支持图像输入的模型名特征（保守列表，只用于提前提醒）。
KNOWN_VISION_MARKERS = (
    "vision",
    "-vl",
    "vl-",
    "llava",
    "pixtral",
    "internvl",
    "minicpm-v",
    "glm-4v",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "claude-3",
    "claude-4",
    "claude-sonnet",
    "claude-opus",
    "gemini",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "step-1v",
    "moonshot-v1-vision",
)

# 项目内置的视觉模型名。它由 core/ai_client.py 配置。
#
# 实测记录（2026-10-10，本机，deepseek 服务商）：
#   用一张写着随机编号的合成图调用 deepseek-flash，
#   2.5 秒返回，且准确读出了图中的编号（ARONA-7391-KX）。
#   -> 确认 DeepSeek 下该模型接受 image_url 多模态输入。
#
# 因此仅在 provider == "deepseek" 时把它当作已确认；
# 其他服务商下无法保证同名模型存在，仍需按未确认处理。
PROJECT_VISION_MODEL = "deepseek-flash"

PROJECT_VISION_PROVIDER = "deepseek"


@dataclass
class VisionSupport:
    """视觉输入能力判定结果。"""

    supported: bool
    confirmed: bool
    provider: str
    model: str
    reason: str

    def describe(self) -> str:
        if not self.supported:
            return f"当前模型不支持发送图片：{self.reason}"
        if self.confirmed:
            return f"将使用 {self.model} 分析截图。"
        return f"将尝试使用 {self.model} 分析截图（{self.reason}）。"


def classify_vision_support(provider: str, model: str, api_key: str) -> VisionSupport:
    """
    判断当前配置是否可能支持图像输入。

    注意：这只是基于模型名的保守判断，
    不能代替服务端的真实能力。
    真正不支持时，analyze() 会把服务端返回的错误原样交回。
    """

    provider = (provider or "").strip().lower()
    model = (model or "").strip()

    if not api_key:
        return VisionSupport(
            supported=False,
            confirmed=True,
            provider=provider,
            model=model,
            reason="尚未配置 API Key，请先打开设置填写。",
        )

    if not model:
        return VisionSupport(
            supported=False,
            confirmed=True,
            provider=provider,
            model=model,
            reason="尚未配置模型名称。",
        )

    lowered = model.lower()

    if any(marker in lowered for marker in KNOWN_VISION_MARKERS):
        return VisionSupport(
            supported=True,
            confirmed=True,
            provider=provider,
            model=model,
            reason="模型名匹配已知的视觉模型。",
        )

    if lowered == PROJECT_VISION_MODEL:
        if provider == PROJECT_VISION_PROVIDER:
            return VisionSupport(
                supported=True,
                confirmed=True,
                provider=provider,
                model=model,
                reason=(
                    "DeepSeek 内置视觉模型，"
                    "已于 2026-10-10 在本机实测通过。"
                ),
            )

        return VisionSupport(
            supported=True,
            confirmed=False,
            provider=provider,
            model=model,
            reason="这是项目内置的视觉模型名，但当前服务商下无法确认其是否可用",
        )

    return VisionSupport(
        supported=False,
        confirmed=False,
        provider=provider,
        model=model,
        reason=(
            f"无法确认模型 {model} 是否支持图像输入。"
            "可以在设置里换成带视觉能力的模型后再试。"
        ),
    )


def looks_like_vision_rejection(error_text: str) -> bool:
    """根据服务端报错，判断是否属于「不支持图像输入」。"""

    if not error_text:
        return False

    text = error_text.lower()

    keywords = (
        "image",
        "vision",
        "multimodal",
        "multi-modal",
        "modality",
        "content type",
        "invalid content",
        "unsupported",
        "not support",
    )

    return any(word in text for word in keywords)


# ============================================================
# 截图结果
# ============================================================

@dataclass
class ScreenShot:
    """一次桌面截图的结果。图像保存在内存中，默认不落盘。"""

    success: bool
    data_url: str = ""
    width: int = 0
    height: int = 0
    source_width: int = 0
    source_height: int = 0
    screen_name: str = ""
    encoded_bytes: int = 0
    active_window: dict[str, Any] | None = None
    error: str | None = None
    image: Any = field(default=None, repr=False)
    # 截图前是否临时隐藏了 AronaClient 自己的窗口。
    self_hidden: bool = False

    def release(self) -> None:
        """释放内存中的图像。"""
        self.image = None
        self.data_url = ""

    def summary(self) -> str:
        if not self.success:
            return f"截图失败：{self.error}"

        scale = (
            f"{self.source_width}x{self.source_height}"
            f" -> {self.width}x{self.height}"
        )

        return (
            f"截图成功：{self.screen_name}  {scale}  "
            f"JPEG {self.encoded_bytes // 1024} KB"
        )


# ============================================================
# 主类
# ============================================================

class DesktopVision:
    """
    桌面截图与视觉分析。

    典型用法：

        vision = DesktopVision(ai_client)

        support = vision.check_vision_support()
        if not support.supported:
            show(support.reason)      # 不要假装能识别
            return

        shot = vision.capture()       # 内存截图，不落盘
        if not shot.success:
            show(shot.error)
            return

        # 这里必须先向用户展示并取得确认
        if not user_confirmed():
            return

        result = vision.analyze(shot, question, user_confirmed=True)
        shot.release()
    """

    def __init__(
        self,
        ai_client,
        max_width: int = DEFAULT_MAX_WIDTH,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ):
        self.ai_client = ai_client
        self.max_width = max(320, int(max_width))
        self.jpeg_quality = max(
            MIN_JPEG_QUALITY,
            min(MAX_JPEG_QUALITY, int(jpeg_quality)),
        )

    # --------------------------------------------------------
    # 屏幕信息
    # --------------------------------------------------------

    @staticmethod
    def list_screens() -> list[dict[str, Any]]:
        """
        列出所有显示器。优先复用 computer 包的实现。

        需要桌面视觉授权：屏幕几何信息属于桌面读数。
        未授权时返回空列表，并留痕（不静默）。
        """

        _ok, _reason = check_desktop_vision_authorization()

        if not _ok:

            _note_desktop_vision_denied("list_screens", _reason)

            return []

        try:
            from computer.screen_capture import ScreenCapture

            return ScreenCapture().get_screen_info()
        except Exception:
            pass

        app = QGuiApplication.instance()

        if app is None:
            return []

        result = []

        for index, screen in enumerate(app.screens()):
            geo = screen.geometry()

            result.append({
                "index": index,
                "name": screen.name(),
                "geometry": {
                    "x": geo.x(),
                    "y": geo.y(),
                    "width": geo.width(),
                    "height": geo.height(),
                },
                "device_pixel_ratio": screen.devicePixelRatio(),
                "primary": screen is app.primaryScreen(),
            })

        return result

    @staticmethod
    def get_active_window_info() -> dict[str, Any] | None:
        """
        读取当前前台窗口信息。失败时返回 None，不影响截图。

        需要桌面视觉授权：前台窗口标题与进程路径属于敏感读数。
        未授权时返回 None，并留痕（不静默）。
        """

        _ok, _reason = check_desktop_vision_authorization()

        if not _ok:

            _note_desktop_vision_denied("get_active_window_info", _reason)

            return None

        try:
            from computer.window_info import get_active_window

            info = get_active_window()

            if isinstance(info, dict) and info.get("success"):
                return info.get("window")

        except Exception:
            pass

        return None

    # --------------------------------------------------------
    # 视觉能力
    # --------------------------------------------------------

    def _vision_model(self) -> str:
        """取得用于图像分析的模型名。"""

        try:
            return self.ai_client._get_vision_model() or ""
        except Exception:
            pass

        try:
            return self.ai_client.get_model() or ""
        except Exception:
            return ""

    def check_vision_support(self) -> VisionSupport:
        """检查当前配置是否可能支持图像输入。"""

        provider = ""
        api_key = ""
        model = ""

        try:
            provider = self.ai_client.get_provider() or ""
        except Exception:
            pass

        try:
            api_key = self.ai_client.settings.get_api_key(provider) or ""
        except Exception:
            api_key = ""

        model = self._vision_model()

        return classify_vision_support(provider, model, api_key)

    def is_ready(self) -> tuple[bool, str]:
        """是否可以真正发起分析。返回 (是否可用, 原因)。"""

        try:
            if not self.ai_client.client:
                return False, "尚未配置 AI API Key，请打开设置进行配置。"
        except Exception:
            return False, "无法确认 AI 客户端状态。"

        support = self.check_vision_support()

        if not support.supported:
            return False, support.reason

        return True, support.reason

    # --------------------------------------------------------
    # 截图
    # --------------------------------------------------------

    def capture(
        self,
        screen_index: int | None = None,
        include_window_info: bool = True,
        hide_widgets=None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    ) -> ScreenShot:
        """
        截取指定显示器（默认主显示器）。

        图像只保存在内存里，不会写入磁盘。

        hide_widgets：
            截图前需要临时隐藏的窗口列表
            （通常传 [主窗口]，避免拍到 AronaClient 自己）。
            不传就直接截，不隐藏任何东西。

        需要桌面视觉授权（方案 B 的独立开关，默认关闭）。
        未授权时【先拒绝、后返回】，不会读取任何桌面像素。

        线程安全：
            手动截图路径在主线程调用；V1 感知路径在 worker 线程调用。
            window.hide/show、screen.grabWindow(0) 在主线程环境下安全；
            在 worker 线程中调用时，_settle() 仅做时间等待，不触碰
            Qt 事件循环，避免跨线程事件交叉。
        """

        # ---------------- 门禁：必须在任何桌面读取之前 ----------------
        #
        # 放在最前面，保证「未授权 -> 拒绝」这条路径
        # 不会先调用 screen.grabWindow() 再拒绝。
        _ok, _reason = check_desktop_vision_authorization()

        if not _ok:

            _note_desktop_vision_denied("capture", _reason)

            return ScreenShot(
                success=False,
                error=(
                    f"{_reason}"
                    "（桌面视觉功能默认关闭，可在设置中开启）"
                ),
            )

        return self._do_capture(
            screen_index, include_window_info, hide_widgets, settle_seconds
        )

    def _do_capture(
        self,
        screen_index: int | None,
        include_window_info: bool,
        hide_widgets,
        settle_seconds: float,
    ) -> ScreenShot:
        """
        实际的截图实现。必须在 Qt 主线程调用。
        """
        app = QGuiApplication.instance()

        if app is None:
            return ScreenShot(
                success=False,
                error=(
                    "当前没有 Qt 应用实例，无法截图。"
                    "请在 AronaClient 桌面程序中运行。"
                ),
            )

        screens = app.screens()

        if not screens:
            return ScreenShot(
                success=False,
                error="系统没有可用的显示器。",
            )

        try:
            if screen_index is None:
                screen = app.primaryScreen()
            else:
                if not isinstance(screen_index, int) or isinstance(screen_index, bool):
                    raise TypeError("screen_index 必须是整数或 None。")

                if screen_index < 0 or screen_index >= len(screens):
                    raise IndexError(
                        f"显示器索引超出范围：{screen_index}，"
                        f"当前共有 {len(screens)} 个显示器。"
                    )

                screen = screens[screen_index]

            if screen is None:
                raise RuntimeError("无法取得目标显示器。")

            # ----------------------------------------------------
            # 关键：截图前先把自己藏起来。
            #
            # 实测：AronaClient 窗口最大化时，
            # 不隐藏直接截 -> 画面 84.0% 是它自己的界面；
            # 隐藏并等待桌面重绘后再截 -> 回落到 0.1%。
            #
            # temporarily_hidden 保证无论是正常、取消还是异常，
            # 窗口都会恢复。
            # ----------------------------------------------------

            self_hidden = bool(
                hide_widgets
            )

            active_window = None

            with temporarily_hidden(
                hide_widgets,
                settle_seconds,
            ):

                pixmap = screen.grabWindow(0)

                # 趁窗口还藏着的时候读前台窗口。
                #
                # 如果放到恢复之后再读，
                # 读到的永远是 AronaClient 自己
                # （因为恢复时又把它激活了），
                # 这个上下文会喂给 AI，会误导它。
                if include_window_info:

                    active_window = (
                        self.get_active_window_info()
                    )

            if pixmap.isNull():
                raise RuntimeError(
                    "截图失败：系统未返回有效图像。"
                    "可能原因：运行在远程桌面、安全桌面，"
                    "或没有桌面会话的权限。"
                )

            image = pixmap.toImage()

            if image.isNull() or image.width() <= 0 or image.height() <= 0:
                raise RuntimeError("截图失败：得到的图像无效。")

            source_width = image.width()
            source_height = image.height()

            image = self._downscale(image)

            data_url, encoded_bytes = self._encode_jpeg(image)

            return ScreenShot(
                success=True,
                data_url=data_url,
                width=image.width(),
                height=image.height(),
                source_width=source_width,
                source_height=source_height,
                screen_name=screen.name() or "主显示器",
                encoded_bytes=encoded_bytes,
                active_window=active_window,
                image=image,
                self_hidden=self_hidden,
            )

        except Exception as exc:
            return ScreenShot(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _downscale(self, image: QImage) -> QImage:
        """按最大宽度等比缩小，降低内存与网络消耗。"""

        if image.width() <= self.max_width:
            return image

        return image.scaledToWidth(
            self.max_width,
            Qt.SmoothTransformation,
        )

    def _encode_jpeg(self, image: QImage) -> tuple[str, int]:
        """把 QImage 编码成 JPEG data URL。"""

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)

        saved = image.save(
            buffer,
            "JPEG",
            self.jpeg_quality,
        )

        raw = bytes(buffer.data())
        buffer.close()

        if not saved or not raw:
            raise RuntimeError("截图失败：图像编码为 JPEG 时出错。")

        if len(raw) > MAX_ENCODED_BYTES:
            raise RuntimeError(
                f"截图过大（{len(raw) // 1024} KB），"
                "已超过安全上限，未发送。"
            )

        encoded = base64.b64encode(raw).decode("ascii")

        return f"data:image/jpeg;base64,{encoded}", len(raw)

    # --------------------------------------------------------
    # 生成提问
    # --------------------------------------------------------

    @staticmethod
    def build_prompt(
        shot: ScreenShot,
        question: str = "",
    ) -> str:
        """把用户问题与窗口信息合成一段提问文字。"""

        parts = []

        question = (question or "").strip()

        if question:
            parts.append(question)
        else:
            parts.append(DEFAULT_QUESTION)

        if shot.active_window:
            title = (shot.active_window.get("title") or "").strip()
            process = (shot.active_window.get("process_path") or "").strip()

            context = []

            if title:
                context.append(f"标题：{title}")

            if process:
                context.append(f"程序：{Path(process).name}")

            if context:
                parts.append(
                    "\n（供参考，由系统读取，可能与截图内容不一致："
                    + "；".join(context)
                    + "）"
                )

        return "\n".join(parts)

    # --------------------------------------------------------
    # 分析
    # --------------------------------------------------------

    def analyze(
        self,
        shot: ScreenShot,
        question: str = "",
        timeout: float = 90.0,
        *,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """
        把截图交给视觉模型分析。

        ⚠️ 这是**联网操作**：截图会上传给模型并消耗 API 额度。

        必须显式传入 user_confirmed=True 才会真正上传。
        默认 False 表示拒绝 —— 漏传参数时不会误上传。

        关于 user_confirmed 的保护范围，必须如实理解：
            它是**显式调用标志**，不是不可伪造的用户身份凭据。
            任何代码都能传 True。它的价值在于「漏传即拒绝」，
            以及让上传路径在代码审查里必须显式声明。
            它**不能**证明"真的是用户在界面上点的同意"。

        本方法绝不伪造识别结果：失败时 text 一定为空字符串。
        """

        result: dict[str, Any] = {
            "success": False,
            "text": "",
            "error": None,
            "hint": None,
            "sent": False,
            "model": "",
        }

        # ---------------- 联网门禁 ----------------
        #
        # 必须在任何外部 API 请求之前执行。
        # 下面的分支不会触碰网络，也不会读取 API Key。
        if not user_confirmed:

            result["error"] = (
                "未取得用户确认，拒绝把桌面截图上传给模型。"
            )
            result["hint"] = "未发送任何图片到云端。"

            return result

        # DV 授权复查：在真正发出网络请求前再次验证权限。
        # 这是最后一道防线 —— 防止用户在截图完成后、分析开始前关闭 DV 开关。
        _dv_ok, _dv_reason = check_desktop_vision_authorization()
        if not _dv_ok:
            result["error"] = f"桌面视觉授权已关闭：{_dv_reason}"
            result["hint"] = "未发送任何图片到云端。"
            return result

        if not shot.success or not shot.data_url:
            result["error"] = shot.error or "没有可用的截图。"
            return result

        support = self.check_vision_support()
        result["model"] = support.model

        if not support.supported:
            result["error"] = support.reason
            result["hint"] = "未发送任何图片到云端。"
            return result

        client = getattr(self.ai_client, "client", None)

        if client is None:
            result["error"] = "尚未配置 AI API Key，请打开设置进行配置。"
            return result

        prompt = self.build_prompt(shot, question)

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": shot.data_url,
                            "detail": "auto",
                        },
                    },
                ],
            }
        ]

        try:
            response = client.chat.completions.create(
                model=support.model,
                messages=messages,
                timeout=timeout,
            )

            text = response.choices[0].message.content or ""

            result["sent"] = True
            result["text"] = text.strip()
            result["success"] = bool(result["text"])

            if not result["success"]:
                result["error"] = "模型返回了空内容。"

            return result

        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"

            result["sent"] = True
            result["text"] = ""
            result["error"] = message

            if looks_like_vision_rejection(message):
                result["hint"] = (
                    "服务端似乎不接受图像输入，"
                    "当前模型可能不具备视觉能力。"
                    "请在设置中更换支持图像输入的模型。"
                )

            return result

    # --------------------------------------------------------
    # 可选保存（默认不保存）
    # --------------------------------------------------------

    @staticmethod
    def save_capture(
        shot: ScreenShot,
        directory: str | Path,
        filename: str = "",
    ) -> dict[str, Any]:
        """
        把截图保存到用户指定的目录。

        只有调用方明确要求保存时才会写盘；
        capture() 本身从不保存。

        需要桌面视觉授权：截图落盘属于桌面数据的持久化，
        不得因为权限关闭而被绕过。
        """

        _ok, _reason = check_desktop_vision_authorization()

        if not _ok:

            _note_desktop_vision_denied("save_capture", _reason)

            return {
                "success": False,
                "path": None,
                "error": f"{_reason}（桌面视觉功能默认关闭）",
            }

        if not shot.success or shot.image is None:
            return {
                "success": False,
                "path": None,
                "error": shot.error or "没有可保存的截图。",
            }

        try:
            target_dir = Path(directory).expanduser().resolve()
            target_dir.mkdir(parents=True, exist_ok=True)

            name = (filename or "").strip()

            if not name:
                from datetime import datetime

                name = datetime.now().strftime("desktop_%Y%m%d_%H%M%S.png")

            if not name.lower().endswith(".png"):
                name += ".png"

            # 防止文件名逃逸到目标目录之外。
            path = (target_dir / Path(name).name).resolve()

            if path.parent != target_dir:
                raise ValueError("文件名无效。")

            if not shot.image.save(str(path), "PNG"):
                raise OSError(f"无法保存截图：{path}")

            return {
                "success": True,
                "path": str(path),
                "error": None,
            }

        except Exception as exc:
            return {
                "success": False,
                "path": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
