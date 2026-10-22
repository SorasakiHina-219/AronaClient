# ui.py
# AronaClient V0.1
# 原版 UI + 语音转文字 + 多会话管理
# 第一阶段：个性化体验 + 聊天背景 + 用户头像 + 工作空间基础
# 注意：不改变原有 UI 布局

import sys
import ctypes
from ctypes import wintypes
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import (
    QTimer,
    QEvent,
    QObject,
    QThread,
    Qt,
    Signal,
    Slot,
)

from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QPainter,
    QPalette,
    QPixmap,
)

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSlider,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.ai_client import AIClient
from core.speech import SpeechRecognizer

# V0.2 第一阶段：桌面截图与视觉感知。
#
# 这里用 try 包住：
# 万一新模块本身出问题，也只是「桌面截图」按钮不可用，
# 不会影响 AronaClient 正常启动和原有的聊天、语音等功能。
try:

    from core.desktop_vision import DesktopVision

except Exception as _desktop_vision_error:

    DesktopVision = None

    print(
        "桌面感知模块加载失败，该功能将不可用：",
        _desktop_vision_error,
    )

# V0.3 桌面自主感知（持续观察 + AI 分析 + 主动提醒）。
try:

    from core.desktop_awareness import (
        DesktopAwareness,
        AwarenessState,
    )

except Exception as _awareness_error:

    DesktopAwareness = None
    AwarenessState = None

    print(
        "桌面自主感知模块加载失败，该功能将不可用：",
        _awareness_error,
    )

# R12.3-07 M3：图片生成（默认关闭，fail-closed 导入）。
#
# 与桌面感知同款保护：模块本身或依赖出问题，
# 只让「图片生成」按钮不可用，不影响聊天/语音/桌面功能。
try:

    from core.image_generation import (
        ImageGenerationService,
        ImageGenError,
    )

except Exception as _image_gen_error:

    ImageGenerationService = None
    ImageGenError = None

    print(
        "图片生成模块加载失败，该功能将不可用：",
        _image_gen_error,
    )


# V0.1 桌面小人物。
#
# 同样用 try 包住：
# 万一模块本身或素材出问题，
# 也只是「桌面小人物」不可用，
# 不影响 AronaClient 正常启动和原有的聊天、语音等功能。
try:

    from mascot_window import (
        MascotWindow,
        create_mascot,
        log_lines,
    )

except Exception as _mascot_import_error:

    MascotWindow = None
    create_mascot = None

    def log_lines(_lines):
        """小人物模块不可用时的空日志函数，保证调用处不必判空。"""

        return False

    print(
        "桌面小人物模块加载失败，该功能将不可用：",
        _mascot_import_error,
    )


# ============================================================
# 本地数据 / 工作空间
# ============================================================

APP_DIR = Path(__file__).resolve().parent

DATA_DIR = APP_DIR / "data"
CHATS_DIR = DATA_DIR / "chats"
USER_DIR = DATA_DIR / "user"
APPEARANCE_DIR = DATA_DIR / "appearance"

WORKSPACE_DIR = APP_DIR / "workspace"
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
RESULTS_DIR = WORKSPACE_DIR / "results"
TEMP_DIR = WORKSPACE_DIR / "temp"

# R12.3-07 M3：图片生成门禁错误码 -> 用户安全文案（G7 固定分类）。
_IMAGE_GEN_ERR_MESSAGES = {
    "FEATURE_DISABLED": "图片生成未开启，请在设置中启用。",
    "PROVIDER_NOT_REGISTERED": "该图片服务商未配置。",
    "UNSUPPORTED_PROTOCOL": "该协议尚未实现（第一阶段仅 OpenAI）。",
    "CREDENTIAL_MISSING": "缺少 API Key，请打开设置填写。",
    "CONNECT_TARGET_UNBOUND": "无法验证连接目标，已拒绝。",
    "DNS_RESOLUTION_FAILED": "域名解析失败。",
    "CONNECT_PEER_REJECTED": "连接目标校验失败，已拒绝。",
    "TLS_VERIFICATION_FAILED": "TLS 证书验证失败，已拒绝。",
    "REDIRECT_REJECTED": "检测到重定向，已拒绝。",
    "REQUEST_TIMEOUT": "请求超时。",
    "RATE_LIMITED": "服务商限流，请稍后再试。",
    "PROVIDER_ERROR": "服务商异常。",
    "INVALID_IMAGE_RESPONSE": "生成结果无效。",
    "IMAGE_DECODE_FAILED": "图片解码失败，已丢弃。",
    "OUTPUT_TOO_LARGE": "结果超出大小上限。",
    "INTERNAL_ERROR": "内部错误。",
}

APPEARANCE_FILE = APPEARANCE_DIR / "appearance.json"


def ensure_local_directories():
    """
    创建 AronaClient V0.1 的统一本地数据结构。

    data/
        chats/
        user/
            avatar.*
        appearance/
            appearance.json
            chat_background.*

    workspace/
        uploads/
        results/
        temp/

    头像和聊天背景只保存在本机。
    """

    directories = (
        DATA_DIR,
        CHATS_DIR,
        USER_DIR,
        APPEARANCE_DIR,
        WORKSPACE_DIR,
        UPLOADS_DIR,
        RESULTS_DIR,
        TEMP_DIR,
    )

    for directory in directories:
        try:
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception as exc:
            print(
                "创建本地目录失败：",
                directory,
                exc,
            )


ensure_local_directories()


# ============================================================
# 个性化外观默认值
# ============================================================

DEFAULT_APPEARANCE = {
    "user_bubble_color": "#2D6FE2",
    "ai_bubble_color": "#17243A",
    "message_text_color": "#F2F6FD",
    "user_text_color": "#FFFFFF",
    "ai_text_color": "#F2F6FD",
    "bubble_opacity": 98,
    "bubble_radius": 14,
    "chat_background_mode": "default",
    "chat_background_color": "#0B1220",
    "chat_background_image": "",
    "chat_background_opacity": 100,
    "bubble_background_image": "",
    # 桌面透明背景。
    #
    # 以前这两项没有写进这里，
    # 而 load_appearance_settings() 只保留 DEFAULT_APPEARANCE 里已有的键，
    # 所以「桌面透明背景」开关和透明度根本存不下来，
    # 重启后一定会回到「关闭 + 235」。
    "desktop_transparent": False,
    "desktop_background_alpha": 235,
}


# ============================================================
# 本地外观设置
# ============================================================

def load_appearance_settings():
    """
    从本地 JSON 读取个性化设置。

    如果文件不存在或者格式异常，则使用默认设置。
    """

    settings = dict(
        DEFAULT_APPEARANCE
    )

    try:

        if APPEARANCE_FILE.exists():

            with APPEARANCE_FILE.open(
                "r",
                encoding="utf-8",
            ) as file:

                loaded = json.load(
                    file
                )

            if isinstance(
                loaded,
                dict,
            ):

                for key, value in loaded.items():

                    if key in settings:

                        settings[key] = value

    except Exception as exc:

        print(
            "读取外观设置失败，使用默认设置：",
            exc,
        )

    return settings


def save_appearance_settings(
    settings,
):
    """
    将个性化外观设置保存到本地。

    不使用 AI API，也不会上传服务器。
    """

    ensure_local_directories()

    merged = dict(
        DEFAULT_APPEARANCE
    )

    if isinstance(
        settings,
        dict,
    ):

        merged.update(
            settings
        )

    try:

        with APPEARANCE_FILE.open(
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                merged,
                file,
                ensure_ascii=False,
                indent=2,
            )

        return True

    except Exception as exc:

        print(
            "保存外观设置失败：",
            exc,
        )

        return False


def scale_pixmap_to_fill(
    pixmap,
    width,
    height,
):
    """
    等比缩放图片，让它完全铺满 width x height 的区域，
    多出来的部分从中心裁掉。

    为什么不能用原来的 Qt.KeepAspectRatio：

        横图（例如 240x120）缩小到 84x84 时，
        KeepAspectRatio 只会得到 76x38，
        头像圆框里上下会空出一大片。

    为什么不能用 Qt.IgnoreAspectRatio：

        那样会把图片强行拉伸变形。

    这里用的是 KeepAspectRatioByExpanding + 中心裁切：
    图片比例不变（不变形），铺满整个区域（不留空白），
    裁掉的只是四周多余的部分，主体仍然居中。
    """

    if (
        pixmap is None
        or pixmap.isNull()
        or width <= 0
        or height <= 0
    ):

        return QPixmap()

    scaled = pixmap.scaled(
        width,
        height,
        Qt.KeepAspectRatioByExpanding,
        Qt.SmoothTransformation,
    )

    offset_x = max(
        0,
        (scaled.width() - width) // 2,
    )

    offset_y = max(
        0,
        (scaled.height() - height) // 2,
    )

    return scaled.copy(
        offset_x,
        offset_y,
        width,
        height,
    )


def make_circular_pixmap(
    pixmap,
):
    """
    把图片裁成圆形。

    头像控件在样式表里用的是 border-radius
    （#avatarPreview 是 42px，#userAvatar 是 22px），
    但 Qt 的 QLabel 不会用圆角去裁剪 pixmap。

    以前图片比控件小，四方角藏在圆框里面看不出来；
    现在图片铺满了，如果不先裁圆，
    四个方角就会露在圆框外面。

    所以这里手动裁一次，让头像保持原来的圆形外观。
    """

    if pixmap is None or pixmap.isNull():

        return QPixmap()

    width = pixmap.width()
    height = pixmap.height()

    if width <= 0 or height <= 0:

        return QPixmap()

    result = QPixmap(
        width,
        height,
    )

    result.fill(
        Qt.transparent
    )

    painter = QPainter(
        result
    )

    painter.setRenderHint(
        QPainter.Antialiasing,
        True,
    )

    painter.setBrush(
        QBrush(pixmap)
    )

    painter.setPen(
        Qt.NoPen
    )

    painter.drawEllipse(
        0,
        0,
        width,
        height,
    )

    painter.end()

    return result


def normalize_hex_color(
    value,
    fallback,
):
    """
    确保颜色始终是合法的十六进制颜色。
    """

    if not isinstance(
        value,
        str,
    ):

        return fallback

    value = value.strip()

    color = QColor(
        value
    )

    if not color.isValid():

        return fallback

    return color.name(
        QColor.HexRgb
    )


def clamp_int(
    value,
    minimum,
    maximum,
    fallback,
):
    try:

        value = int(
            value
        )

    except Exception:

        return fallback

    return max(
        minimum,
        min(
            maximum,
            value,
        ),
    )


# ============================================================
# AI 后台任务
# ============================================================

class AIWorker(QObject):

    finished = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        client,
        message,
        attachments=None,
    ):
        super().__init__()

        self.client = client
        self.message = message
        self.attachments = attachments or []

    @Slot()
    def run(self):

        try:

            result = self.client.send_message(
                self.message,
                self.attachments,
            )

            self.finished.emit(
                result
            )

        except Exception as exc:

            self.error.emit(
                str(exc)
            )


# ============================================================
# 桌面截图分析后台任务（V0.2 第一阶段）
# ============================================================

class DesktopVisionWorker(QObject):
    """
    在后台线程里执行一次桌面截图分析。

    与 AIWorker 使用同一套线程写法，
    避免在网络请求期间卡住界面。
    """

    finished = Signal(object)

    def __init__(
        self,
        vision,
        shot,
        question,
        user_confirmed=False,
    ):
        super().__init__()

        self.vision = vision
        self.shot = shot
        self.question = question

        # 是否已由用户在 DesktopCaptureDialog 里确认发送。
        #
        # ⚠️ 这是显式调用标志，不是不可伪造的用户身份凭据；
        # 它只保证「没有确认就绝不上传」。
        self.user_confirmed = bool(user_confirmed)

    @Slot()
    def run(self):

        try:

            result = self.vision.analyze(
                self.shot,
                self.question,
                user_confirmed=self.user_confirmed,
            )

        except Exception as exc:

            # 任何意外都转成结构化结果，
            # 绝不伪造识别内容。
            result = {
                "success": False,
                "text": "",
                "error": f"{type(exc).__name__}: {exc}",
                "hint": None,
                "sent": False,
                "model": "",
            }

        self.finished.emit(
            result
        )


# ============================================================
# 语音识别后台任务
# ============================================================

class SpeechWorker(QObject):

    status = Signal(str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self):

        super().__init__()

        self.recognizer = None

    @Slot()
    def run(self):

        try:

            self.status.emit(
                "__LISTENING__"
            )

            self.recognizer = SpeechRecognizer()

            text = self.recognizer.listen(
                timeout=8
            )

            if text:

                self.status.emit(
                    "__RECOGNIZING__"
                )

                self.finished.emit(
                    text
                )

            else:

                self.error.emit(
                    "没有听清楚你说的话，请再试一次。"
                )

        except Exception as exc:

            error_text = str(
                exc
            ).strip()

            if not error_text:

                error_text = (
                    "语音识别失败，请检查麦克风和网络连接。"
                )

            self.error.emit(
                error_text
            )


# ============================================================
# 桌面感知主动回复后台任务
# ============================================================

class AwarenessReplyWorker(QObject):
    """
    将桌面感知事件提交给 AI，获取主动回复。

    复用现有 AIClient 的 send_message 接口，
    与 AIWorker 使用相同的线程管理模式。
    """

    finished = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        ai_client,
        awareness_description,
    ):
        super().__init__()
        self.ai_client = ai_client
        self.awareness_description = awareness_description

    @Slot()
    def run(self):
        try:
            # 将感知结果作为上下文发送给 AI，请求简短回应
            message = (
                "[桌面感知摘要]\n"
                "{0}\n\n"
                "请根据以上桌面观察结果，用一两句话给出相关提醒或建议。"
                "如果 observation 中没有需要特别关注的变化，"
                "可以简短回应 '一切正常'。".format(
                    self.awareness_description
                )
            )
            result = self.ai_client.send_message(
                message,
                save_to_session=False,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


# ============================================================
# 图片生成后台任务（R12.3-07 M3 / G9）
# ============================================================

class ImageGenWorker(QObject):
    """
    在后台线程里执行一次图片生成。

    与 AwarenessReplyWorker 使用同一套 QThread + moveToThread 写法，
    避免网络请求期间卡住界面。所有安全门禁与连接绑定逻辑在
    core.image_generation 内（M1），这里只负责线程化与结果回传。
    """

    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        service,
        prompt,
        size="1024x1024",
        quality="low",
        model="",
    ):
        super().__init__()
        self.service = service
        self.prompt = prompt
        self.size = size
        self.quality = quality
        self.model = model

    @Slot()
    def run(self):
        try:
            path = self.service.generate(
                prompt=self.prompt,
                size=self.size,
                quality=self.quality,
                model=self.model,
            )
            self.finished.emit(path)
        except Exception as exc:
            # G7：只回传固定分类的用户安全消息，不回传 SDK 原文。
            msg = getattr(exc, "user_message", None)
            if not msg:
                msg = str(exc)
            self.error.emit(msg)


# ============================================================
# 设置窗口
# ============================================================

class SettingsDialog(QDialog):

    saved = Signal()
    appearance_saved = Signal(bool, int)
    awareness_toggled = Signal(bool)

    def __init__(
        self,
        ai_client,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.setAttribute(
            Qt.WA_DeleteOnClose
        )

        self.ai_client = ai_client
        self.settings = ai_client.settings

        self.setWindowTitle(
            "AronaClient 设置"
        )

        self.setMinimumWidth(
            520
        )

        self.setMaximumWidth(
            520
        )

        self.setMinimumHeight(
            420
        )

        # ----------------------------------------------------
        # 个性化设置暂存
        # ----------------------------------------------------

        self.appearance_settings = (
            dict(
                getattr(
                    parent,
                    "appearance_settings",
                    DEFAULT_APPEARANCE,
                )
                if parent is not None
                else DEFAULT_APPEARANCE
            )
        )

        self.appearance_settings = (
            self._normalize_appearance_settings(
                self.appearance_settings
            )
        )

        self.selected_avatar_path = (
            self._get_existing_avatar_path()
        )

        self.selected_background_path = (
            self.appearance_settings.get(
                "chat_background_image",
                "",
            )
        )

        self.setStyleSheet(
            """
            QDialog {
                background: #000000;
                color: #F5F7FA;
            }

            QLabel {
                color: #F5F7FA;
            }

            #settingsTitle {
                color: #FFFFFF;
            }

            #settingsSubtitle {
                color: #C9D1DC;
            }

            #settingsCard {
                background: #0A0A0A;
                border: 1px solid #252525;
                border-radius: 12px;
            }

            #settingsSectionTitle {
                color: #FFFFFF;
            }

            QLineEdit,
            QComboBox {
                background: #111111;
                border: 1px solid #353535;
                border-radius: 8px;
                padding: 9px;
                color: #FFFFFF;
                selection-background-color: #315f9f;
                selection-color: #FFFFFF;
            }

            QLineEdit:focus,
            QComboBox:focus {
                border: 1px solid #4A8DF4;
            }

            QComboBox QAbstractItemView {
                background: #111111;
                color: #FFFFFF;
                border: 1px solid #353535;
                selection-background-color: #263F62;
                selection-color: #FFFFFF;
            }

            QCheckBox {
                color: #F1F5F9;
                spacing: 8px;
            }

            QCheckBox:disabled {
                color: #707070;
            }

            QPushButton {
                background: #151515;
                color: #F5F7FA;
                border: 1px solid #333333;
                border-radius: 8px;
                padding: 9px 16px;
            }

            QPushButton:hover {
                background: #202020;
            }

            QPushButton:pressed {
                background: #101010;
            }

            #primaryButton {
                background: #3C7FF0;
                color: #FFFFFF;
                border: none;
            }

            #primaryButton:hover {
                background: #4C8CF4;
            }

            #settingsStatus {
                color: #E5E7EB;
            }

            #settingsSectionTitle {
                color: #FFFFFF;
                font-size: 14px;
                font-weight: 700;
            }

            #changelogCard {
                background: #080808;
                border: 1px solid #252525;
                border-radius: 12px;
            }

            #changelogTitle {
                color: #FFFFFF;
                font-size: 15px;
                font-weight: 700;
            }

            #changelogVersion {
                color: #69A9FF;
                font-size: 13px;
                font-weight: 700;
            }

            #changelogText {
                color: #D7DEE8;
                font-size: 13px;
            }

            #avatarPreview {
                background: #17243A;
                border: 1px solid #33496C;
                border-radius: 42px;
            }

            #colorPreview {
                border: 1px solid #3A4C67;
                border-radius: 8px;
            }

            QSlider::groove:horizontal {
                height: 5px;
                background: #303030;
                border-radius: 2px;
            }

            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                background: #4B8CF5;
                border-radius: 8px;
            }

            QSlider::sub-page:horizontal {
                background: #3C7FF0;
                border-radius: 2px;
            }

            QScrollArea {
                background: transparent;
                border: none;
            }

            QScrollArea > QWidget {
                background: transparent;
                border: none;
            }

            QScrollArea QWidget#settingsScrollContent {
                background: transparent;
                border: none;
            }

            QScrollBar:vertical {
                width: 8px;
                background: #000000;
                margin: 0px;
            }

            QScrollBar::handle:vertical {
                background: #353535;
                border-radius: 4px;
                min-height: 30px;
            }

            QScrollBar::handle:vertical:hover {
                background: #4A4A4A;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }

            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )

        root = QVBoxLayout(
            self
        )

        root.setContentsMargins(
            28,
            20,
            28,
            20,
        )

        root.setSpacing(
            10
        )

        title = QLabel(
            "设置"
        )

        title.setObjectName(
            "settingsTitle"
        )

        root.addWidget(
            title
        )

        subtitle = QLabel(
            "配置 AI 服务商、模型、API Key 和 AronaClient 个性化外观。"
            "API Key 会保存到 Windows 凭据管理器。"
        )

        subtitle.setObjectName(
            "settingsSubtitle"
        )

        subtitle.setWordWrap(
            True
        )

        root.addWidget(
            subtitle
        )

        self.scroll_area = QScrollArea()

        self.scroll_area.setObjectName(
            "settingsScrollArea"
        )

        self.scroll_area.setWidgetResizable(
            True
        )

        self.scroll_area.setFrameShape(
            QFrame.NoFrame
        )

        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )

        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded
        )

        self.scroll_area.setWidget(
            self._build_scroll_content()
        )

        root.addWidget(
            self.scroll_area,
            1,
        )

        # ----------------------------------------------------
        # 底部按钮
        #
        # 按钮放在滚动区域「外面」，
        # 固定在对话框最底部。
        #
        # 这样设置项再多、窗口再小，
        # 「取消 / 保存」都始终可见、可点击，
        # 不会被滚动内容顶走，也不会被裁切。
        # ----------------------------------------------------

        root.addLayout(
            self._build_dialog_buttons()
        )

        self.load_current()

    # ========================================================
    # 底部按钮
    # ========================================================

    def _build_dialog_buttons(
        self,
    ):
        """
        创建设置对话框底部的「取消 / 保存」按钮行。

        这一行由 __init__ 加到 root 布局里，
        位于 QScrollArea 之外，因此不随内容滚动。
        """

        buttons = QHBoxLayout()

        buttons.addStretch()

        cancel_button = QPushButton(
            "取消"
        )

        cancel_button.clicked.connect(
            self.reject
        )

        save_button = QPushButton(
            "保存"
        )

        save_button.setObjectName(
            "primaryButton"
        )

        save_button.clicked.connect(
            self.save_settings
        )

        buttons.addWidget(
            cancel_button
        )

        buttons.addWidget(
            save_button
        )

        return buttons

    # ========================================================
    # 外观设置辅助
    # ========================================================

    def _normalize_appearance_settings(
        self,
        settings,
    ):

        normalized = dict(
            DEFAULT_APPEARANCE
        )

        if isinstance(
            settings,
            dict,
        ):

            normalized.update(
                settings
            )

        normalized[
            "user_bubble_color"
        ] = normalize_hex_color(
            normalized.get(
                "user_bubble_color"
            ),
            DEFAULT_APPEARANCE[
                "user_bubble_color"
            ],
        )

        normalized[
            "ai_bubble_color"
        ] = normalize_hex_color(
            normalized.get(
                "ai_bubble_color"
            ),
            DEFAULT_APPEARANCE[
                "ai_bubble_color"
            ],
        )

        normalized[
            "message_text_color"
        ] = normalize_hex_color(
            normalized.get(
                "message_text_color"
            ),
            DEFAULT_APPEARANCE[
                "message_text_color"
            ],
        )

        normalized[
            "user_text_color"
        ] = normalize_hex_color(
            normalized.get(
                "user_text_color"
            ),
            DEFAULT_APPEARANCE[
                "user_text_color"
            ],
        )

        normalized[
            "ai_text_color"
        ] = normalize_hex_color(
            normalized.get(
                "ai_text_color"
            ),
            DEFAULT_APPEARANCE[
                "ai_text_color"
            ],
        )

        normalized[
            "chat_background_color"
        ] = normalize_hex_color(
            normalized.get(
                "chat_background_color"
            ),
            DEFAULT_APPEARANCE[
                "chat_background_color"
            ],
        )

        normalized[
            "bubble_opacity"
        ] = clamp_int(
            normalized.get(
                "bubble_opacity",
                98,
            ),
            20,
            100,
            98,
        )

        normalized[
            "bubble_radius"
        ] = clamp_int(
            normalized.get(
                "bubble_radius",
                14,
            ),
            0,
            40,
            14,
        )

        normalized[
            "chat_background_opacity"
        ] = clamp_int(
            normalized.get(
                "chat_background_opacity",
                100,
            ),
            0,
            100,
            100,
        )

        mode = normalized.get(
            "chat_background_mode",
            "default",
        )

        if mode not in (
            "default",
            "color",
            "image",
        ):

            mode = "default"

        normalized[
            "chat_background_mode"
        ] = mode

        return normalized

    def _get_existing_avatar_path(
        self,
    ):

        for suffix in (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        ):

            path = (
                USER_DIR
                / f"avatar{suffix}"
            )

            if path.exists():

                return str(
                    path
                )

        return ""

    def _set_color_button(
        self,
        button,
        color,
    ):

        button.setStyleSheet(
            f"""
            QPushButton {{
                background: {color};
                color: #FFFFFF;
                border: 1px solid #4A5A73;
                border-radius: 8px;
                padding: 8px 12px;
            }}

            QPushButton:hover {{
                border: 1px solid #78A9E8;
            }}
            """
        )

    def _add_color_setting(
        self,
        layout,
        label_text,
        key,
    ):

        row = QHBoxLayout()

        label = QLabel(
            label_text
        )

        button = QPushButton(
            self.appearance_settings.get(
                key,
                DEFAULT_APPEARANCE.get(
                    key,
                    "#FFFFFF",
                ),
            )
        )

        button.setMinimumWidth(
            110
        )

        self._set_color_button(
            button,
            button.text(),
        )

        def choose_color(
            checked=False,
            target_key=key,
            target_button=button,
        ):

            current = QColor(
                self.appearance_settings.get(
                    target_key,
                    "#FFFFFF",
                )
            )

            color = QColorDialog.getColor(
                current,
                self,
                f"选择{label_text}",
            )

            if not color.isValid():

                return

            value = color.name(
                QColor.HexRgb
            )

            self.appearance_settings[
                target_key
            ] = value

            target_button.setText(
                value
            )

            self._set_color_button(
                target_button,
                value,
            )

        button.clicked.connect(
            choose_color
        )

        row.addWidget(
            label
        )

        row.addStretch()

        row.addWidget(
            button
        )

        layout.addLayout(
            row
        )

        return button

    # ========================================================
    # 设置窗口滚动内容
    # ========================================================

    def _build_scroll_content(
        self,
    ):

        content = QWidget()

        content.setObjectName(
            "settingsScrollContent"
        )

        content_layout = QVBoxLayout(
            content
        )

        content_layout.setContentsMargins(
            0,
            4,
            0,
            10,
        )

        content_layout.setSpacing(
            12
        )

        # ----------------------------------------------------
        # AI 服务设置
        # ----------------------------------------------------

        form = QFrame()

        form.setObjectName(
            "settingsCard"
        )

        form_layout = QVBoxLayout(
            form
        )

        form_layout.setContentsMargins(
            20,
            20,
            20,
            20,
        )

        form_layout.setSpacing(
            12
        )

        provider_label = QLabel(
            "AI 服务商"
        )

        self.provider = QComboBox()

        self.provider.addItem(
            "DeepSeek",
            "deepseek",
        )

        self.provider.addItem(
            "OpenRouter",
            "openrouter",
        )

        model_label = QLabel(
            "模型"
        )

        self.model = QLineEdit()

        key_label = QLabel(
            "API Key"
        )

        self.api_key = QLineEdit()

        self.api_key.setEchoMode(
            QLineEdit.Password
        )

        self.api_key.setPlaceholderText(
            "在这里输入 API Key，不需要修改 Python 文件"
        )

        self.provider.currentIndexChanged.connect(
            self.on_provider_changed
        )

        form_layout.addWidget(
            provider_label
        )

        form_layout.addWidget(
            self.provider
        )

        form_layout.addWidget(
            model_label
        )

        form_layout.addWidget(
            self.model
        )

        form_layout.addWidget(
            key_label
        )

        form_layout.addWidget(
            self.api_key
        )

        content_layout.addWidget(
            form
        )

        # ----------------------------------------------------
        # 用户资料
        # ----------------------------------------------------

        profile_card = QFrame()

        profile_card.setObjectName(
            "settingsCard"
        )

        profile_layout = QVBoxLayout(
            profile_card
        )

        profile_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        profile_layout.setSpacing(
            10
        )

        profile_title = QLabel(
            "用户资料"
        )

        profile_title.setObjectName(
            "settingsSectionTitle"
        )

        profile_layout.addWidget(
            profile_title
        )

        profile_row = QHBoxLayout()

        self.avatar_preview = QLabel(
            "●"
        )

        self.avatar_preview.setObjectName(
            "avatarPreview"
        )

        self.avatar_preview.setAlignment(
            Qt.AlignCenter
        )

        self.avatar_preview.setFixedSize(
            84,
            84,
        )

        profile_row.addWidget(
            self.avatar_preview
        )

        profile_text = QVBoxLayout()

        avatar_hint = QLabel(
            "用户头像仅保存到本机。\n"
            "支持 PNG / JPG / JPEG / WEBP。\n"
            "不会上传到 AI 服务商。"
        )

        avatar_hint.setObjectName(
            "settingsSubtitle"
        )

        avatar_hint.setWordWrap(
            True
        )

        profile_text.addWidget(
            avatar_hint
        )

        avatar_buttons = QHBoxLayout()

        choose_avatar_button = QPushButton(
            "更换头像"
        )

        choose_avatar_button.clicked.connect(
            self.choose_avatar
        )

        reset_avatar_button = QPushButton(
            "恢复默认"
        )

        reset_avatar_button.clicked.connect(
            self.reset_avatar
        )

        avatar_buttons.addWidget(
            choose_avatar_button
        )

        avatar_buttons.addWidget(
            reset_avatar_button
        )

        profile_text.addLayout(
            avatar_buttons
        )

        profile_row.addLayout(
            profile_text,
            1,
        )

        profile_layout.addLayout(
            profile_row
        )

        content_layout.addWidget(
            profile_card
        )

        # ----------------------------------------------------
        # 聊天气泡个性化
        # ----------------------------------------------------

        bubble_card = QFrame()

        bubble_card.setObjectName(
            "settingsCard"
        )

        bubble_layout = QVBoxLayout(
            bubble_card
        )

        bubble_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        bubble_layout.setSpacing(
            10
        )

        bubble_title = QLabel(
            "聊天气泡"
        )

        bubble_title.setObjectName(
            "settingsSectionTitle"
        )

        bubble_layout.addWidget(
            bubble_title
        )

        self._add_color_setting(
            bubble_layout,
            "用户气泡颜色",
            "user_bubble_color",
        )

        self._add_color_setting(
            bubble_layout,
            "Arona 气泡颜色",
            "ai_bubble_color",
        )

        self._add_color_setting(
            bubble_layout,
            "用户文字颜色",
            "user_text_color",
        )

        self._add_color_setting(
            bubble_layout,
            "Arona 文字颜色",
            "ai_text_color",
        )

        # 气泡透明度
        bubble_opacity_row = QHBoxLayout()

        bubble_opacity_label = QLabel(
            "气泡透明度"
        )

        self.bubble_opacity_slider = QSlider(
            Qt.Horizontal
        )

        self.bubble_opacity_slider.setRange(
            20,
            100,
        )

        self.bubble_opacity_slider.setValue(
            self.appearance_settings[
                "bubble_opacity"
            ]
        )

        self.bubble_opacity_value = QLabel(
            f"{self.bubble_opacity_slider.value()}%"
        )

        self.bubble_opacity_slider.valueChanged.connect(
            lambda value:
            self.bubble_opacity_value.setText(
                f"{value}%"
            )
        )

        bubble_opacity_row.addWidget(
            bubble_opacity_label
        )

        bubble_opacity_row.addWidget(
            self.bubble_opacity_slider,
            1,
        )

        bubble_opacity_row.addWidget(
            self.bubble_opacity_value
        )

        bubble_layout.addLayout(
            bubble_opacity_row
        )

        # 气泡圆角
        radius_row = QHBoxLayout()

        radius_label = QLabel(
            "气泡圆角"
        )

        self.bubble_radius_slider = QSlider(
            Qt.Horizontal
        )

        self.bubble_radius_slider.setRange(
            0,
            40,
        )

        self.bubble_radius_slider.setValue(
            self.appearance_settings[
                "bubble_radius"
            ]
        )

        self.bubble_radius_value = QLabel(
            f"{self.bubble_radius_slider.value()} px"
        )

        self.bubble_radius_slider.valueChanged.connect(
            lambda value:
            self.bubble_radius_value.setText(
                f"{value} px"
            )
        )

        radius_row.addWidget(
            radius_label
        )

        radius_row.addWidget(
            self.bubble_radius_slider,
            1,
        )

        radius_row.addWidget(
            self.bubble_radius_value
        )

        bubble_layout.addLayout(
            radius_row
        )

        bubble_hint = QLabel(
            "用户和 Arona 使用独立颜色与文字颜色。"
            "以后可以在这里继续加入气泡背景图片。"
        )

        bubble_hint.setObjectName(
            "settingsSubtitle"
        )

        bubble_hint.setWordWrap(
            True
        )

        bubble_layout.addWidget(
            bubble_hint
        )

        content_layout.addWidget(
            bubble_card
        )

        # ----------------------------------------------------
        # 聊天背景
        # ----------------------------------------------------

        background_card = QFrame()

        background_card.setObjectName(
            "settingsCard"
        )

        background_layout = QVBoxLayout(
            background_card
        )

        background_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        background_layout.setSpacing(
            10
        )

        background_title = QLabel(
            "聊天背景"
        )

        background_title.setObjectName(
            "settingsSectionTitle"
        )

        background_layout.addWidget(
            background_title
        )

        background_mode_label = QLabel(
            "背景类型"
        )

        self.chat_background_mode = QComboBox()

        self.chat_background_mode.addItem(
            "默认 Arona 背景",
            "default",
        )

        self.chat_background_mode.addItem(
            "纯色背景",
            "color",
        )

        self.chat_background_mode.addItem(
            "自定义图片",
            "image",
        )

        mode_index = (
            self.chat_background_mode.findData(
                self.appearance_settings[
                    "chat_background_mode"
                ]
            )
        )

        if mode_index >= 0:

            self.chat_background_mode.setCurrentIndex(
                mode_index
            )

        background_layout.addWidget(
            background_mode_label
        )

        background_layout.addWidget(
            self.chat_background_mode
        )

        # 背景颜色
        background_color_row = QHBoxLayout()

        background_color_label = QLabel(
            "纯色背景"
        )

        self.background_color_button = QPushButton(
            self.appearance_settings[
                "chat_background_color"
            ]
        )

        self._set_color_button(
            self.background_color_button,
            self.background_color_button.text(),
        )

        self.background_color_button.clicked.connect(
            self.choose_background_color
        )

        background_color_row.addWidget(
            background_color_label
        )

        background_color_row.addStretch()

        background_color_row.addWidget(
            self.background_color_button
        )

        background_layout.addLayout(
            background_color_row
        )

        # 背景图片
        background_image_row = QHBoxLayout()

        self.background_image_label = QLabel(
            "未选择图片"
        )

        self.background_image_label.setWordWrap(
            True
        )

        choose_background_button = QPushButton(
            "选择图片"
        )

        choose_background_button.clicked.connect(
            self.choose_background_image
        )

        clear_background_button = QPushButton(
            "清除图片"
        )

        clear_background_button.clicked.connect(
            self.clear_background_image
        )

        background_image_row.addWidget(
            self.background_image_label,
            1,
        )

        background_image_row.addWidget(
            choose_background_button
        )

        background_image_row.addWidget(
            clear_background_button
        )

        background_layout.addLayout(
            background_image_row
        )

        # 背景透明度
        background_opacity_row = QHBoxLayout()

        background_opacity_label = QLabel(
            "背景透明度"
        )

        self.background_opacity_slider = QSlider(
            Qt.Horizontal
        )

        self.background_opacity_slider.setRange(
            0,
            100,
        )

        self.background_opacity_slider.setValue(
            self.appearance_settings[
                "chat_background_opacity"
            ]
        )

        self.background_opacity_value = QLabel(
            f"{self.background_opacity_slider.value()}%"
        )

        self.background_opacity_slider.valueChanged.connect(
            lambda value:
            self.background_opacity_value.setText(
                f"{value}%"
            )
        )

        background_opacity_row.addWidget(
            background_opacity_label
        )

        background_opacity_row.addWidget(
            self.background_opacity_slider,
            1,
        )

        background_opacity_row.addWidget(
            self.background_opacity_value
        )

        background_layout.addLayout(
            background_opacity_row
        )

        background_hint = QLabel(
            "背景图片会保存到本机 data/appearance。"
            "透明度只影响聊天背景，不会影响文字和聊天气泡。"
        )

        background_hint.setObjectName(
            "settingsSubtitle"
        )

        background_hint.setWordWrap(
            True
        )

        background_layout.addWidget(
            background_hint
        )

        self.chat_background_mode.currentIndexChanged.connect(
            self.on_background_mode_changed
        )

        content_layout.addWidget(
            background_card
        )

        # ----------------------------------------------------
        # 界面外观
        # ----------------------------------------------------

        appearance_card = QFrame()

        appearance_card.setObjectName(
            "settingsCard"
        )

        appearance_layout = QVBoxLayout(
            appearance_card
        )

        appearance_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        appearance_layout.setSpacing(
            10
        )

        appearance_title = QLabel(
            "界面外观"
        )

        appearance_title.setObjectName(
            "settingsSectionTitle"
        )

        appearance_layout.addWidget(
            appearance_title
        )

        self.desktop_transparent_checkbox = QCheckBox(
            "桌面透明背景"
        )

        self.desktop_transparent_checkbox.setChecked(
            bool(
                getattr(
                    self.parent(),
                    "desktop_transparent",
                    False,
                )
            )
            if self.parent() is not None
            else False
        )

        appearance_layout.addWidget(
            self.desktop_transparent_checkbox
        )

        opacity_row = QHBoxLayout()

        opacity_label = QLabel(
            "背景透明度"
        )

        self.opacity_slider = QSlider(
            Qt.Horizontal
        )

        self.opacity_slider.setRange(
            70,
            98,
        )

        current_alpha = (
            int(
                getattr(
                    self.parent(),
                    "desktop_background_alpha",
                    235,
                )
            )
            if self.parent() is not None
            else 235
        )

        self.opacity_slider.setValue(
            max(
                70,
                min(
                    98,
                    round(
                        current_alpha
                        * 100
                        / 255
                    ),
                ),
            )
        )

        self.opacity_value = QLabel()

        self.opacity_value.setMinimumWidth(
            45
        )

        def update_opacity_label(
            value,
        ):

            self.opacity_value.setText(
                f"{value}%"
            )

        self.opacity_slider.valueChanged.connect(
            update_opacity_label
        )

        update_opacity_label(
            self.opacity_slider.value()
        )

        opacity_row.addWidget(
            opacity_label
        )

        opacity_row.addWidget(
            self.opacity_slider,
            1,
        )

        opacity_row.addWidget(
            self.opacity_value
        )

        appearance_layout.addLayout(
            opacity_row
        )

        appearance_hint = QLabel(
            "默认关闭以保证低配置设备流畅。"
            "开启后，Windows 桌面会显示在 AronaClient 后方；"
            "聊天气泡、输入框和侧边栏保持清晰。"
        )

        appearance_hint.setObjectName(
            "settingsSubtitle"
        )

        appearance_hint.setWordWrap(
            True
        )

        appearance_layout.addWidget(
            appearance_hint
        )

        content_layout.addWidget(
            appearance_card
        )

        # ----------------------------------------------------
        # 桌面感知
        #
        # 独立开关，默认【关闭】。
        #
        # 与「电脑控制」互不相干：
        #   电脑控制 -> 能不能模拟鼠标键盘
        #   桌面感知 -> 能不能读取桌面截图与前台窗口信息
        #
        # 关闭时，截图 / 屏幕列表 / 前台窗口信息 / 截图落盘
        # 四条路径全部拒绝，且不会先截图再拒绝。
        # ----------------------------------------------------

        desktop_vision_card = QFrame()

        desktop_vision_card.setObjectName(
            "settingsCard"
        )

        desktop_vision_layout = QVBoxLayout(
            desktop_vision_card
        )

        desktop_vision_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        desktop_vision_layout.setSpacing(
            10
        )

        desktop_vision_title = QLabel(
            "桌面感知"
        )

        desktop_vision_title.setObjectName(
            "settingsSectionTitle"
        )

        desktop_vision_layout.addWidget(
            desktop_vision_title
        )

        self.desktop_vision_checkbox = QCheckBox(
            "允许读取桌面截图与前台窗口信息"
        )

        self.desktop_vision_checkbox.setChecked(
            bool(
                self.settings.get_desktop_vision_enabled()
            )
        )

        desktop_vision_layout.addWidget(
            self.desktop_vision_checkbox
        )

        desktop_vision_hint = QLabel(
            "默认关闭。关闭时 Arona 无法截取桌面画面，"
            "也无法读取前台窗口标题。"
            "即使已开启电脑控制，本项仍保持独立，不会自动打开。"
        )

        desktop_vision_hint.setWordWrap(
            True
        )

        desktop_vision_hint.setObjectName(
            "settingsHint"
        )

        desktop_vision_layout.addWidget(
            desktop_vision_hint
        )

        content_layout.addWidget(
            desktop_vision_card
        )

        # ----------------------------------------------------
        # V0.3 桌面自主感知（持续观察）
        # ----------------------------------------------------
        if DesktopAwareness is not None:

            awareness_card = QFrame()
            awareness_card.setObjectName("settingsCard")
            awareness_layout = QVBoxLayout(awareness_card)
            awareness_layout.setContentsMargins(
                16, 14, 16, 14
            )
            awareness_layout.setSpacing(
                8
            )

            awareness_title = QLabel(
                "桌面自主感知（V0.3）"
            )
            awareness_title.setObjectName(
                "settingsTitle"
            )
            awareness_layout.addWidget(
                awareness_title
            )

            awareness_hint = QLabel(
                "开启后，Arona 会按固定间隔自动观察桌面变化，"
                "在发现重要变化时主动提醒你。"
                "不会自动操作鼠标或键盘。"
                "检测到变化时，截图将发送给 AI 模型分析并消耗 API 额度。"
                "采集间隔可在下方设置。"
            )
            awareness_hint.setWordWrap(True)
            awareness_hint.setObjectName("settingsHint")
            awareness_layout.addWidget(
                awareness_hint
            )

            # 开关
            self.awareness_enabled_checkbox = QCheckBox(
                "启用桌面自主感知"
            )
            awareness_layout.addWidget(
                self.awareness_enabled_checkbox
            )
            # 连接信号：切换开关时立即启停感知引擎，无需等保存。
            # 通过信号通知主窗口（SettingsDialog 不直接持有感知引擎）。
            self.awareness_enabled_checkbox.toggled.connect(
                lambda checked: self.awareness_toggled.emit(checked)
            )

            # 间隔设置
            interval_layout = QHBoxLayout()
            interval_label = QLabel("采集间隔（秒）：")
            self.awareness_interval_spin = QSpinBox()
            self.awareness_interval_spin.setRange(
                5, 600
            )
            self.awareness_interval_spin.setValue(
                int(
                    self.settings.settings
                    .get("desktop_awareness", {})
                    .get("interval_seconds", 30)
                )
            )
            self.awareness_interval_spin.setSingleStep(5)
            interval_layout.addWidget(interval_label)
            interval_layout.addWidget(
                self.awareness_interval_spin
            )
            interval_layout.addStretch()
            awareness_layout.addLayout(interval_layout)

            content_layout.addWidget(
                awareness_card
            )

        # ----------------------------------------------------
        # 文件与工作空间
        # ----------------------------------------------------

        workspace_card = QFrame()

        workspace_card.setObjectName(
            "settingsCard"
        )

        workspace_layout = QVBoxLayout(
            workspace_card
        )

        workspace_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        workspace_layout.setSpacing(
            10
        )

        workspace_title = QLabel(
            "文件与工作空间"
        )

        workspace_title.setObjectName(
            "settingsSectionTitle"
        )

        workspace_layout.addWidget(
            workspace_title
        )

        workspace_info = QLabel(
            "上传文件：workspace/uploads\n"
            "AI 工作结果：workspace/results\n"
            "临时文件：workspace/temp"
        )

        workspace_info.setObjectName(
            "settingsSubtitle"
        )

        workspace_info.setWordWrap(
            True
        )

        workspace_layout.addWidget(
            workspace_info
        )

        workspace_button_row = QHBoxLayout()

        open_workspace_button = QPushButton(
            "打开工作空间"
        )

        open_workspace_button.clicked.connect(
            self.open_workspace
        )

        open_results_button = QPushButton(
            "打开结果文件夹"
        )

        open_results_button.clicked.connect(
            self.open_results
        )

        workspace_button_row.addWidget(
            open_workspace_button
        )

        workspace_button_row.addWidget(
            open_results_button
        )

        workspace_layout.addLayout(
            workspace_button_row
        )

        content_layout.addWidget(
            workspace_card
        )

        # ----------------------------------------------------
        # 更新日志
        # ----------------------------------------------------

        changelog_card = QFrame()

        changelog_card.setObjectName(
            "changelogCard"
        )

        changelog_layout = QVBoxLayout(
            changelog_card
        )

        changelog_layout.setContentsMargins(
            20,
            16,
            20,
            16,
        )

        changelog_layout.setSpacing(
            7
        )

        changelog_title = QLabel(
            "更新日志"
        )

        changelog_title.setObjectName(
            "changelogTitle"
        )

        changelog_layout.addWidget(
            changelog_title
        )

        changelog_version = QLabel(
            "AronaClient V0.1"
        )

        changelog_version.setObjectName(
            "changelogVersion"
        )

        changelog_layout.addWidget(
            changelog_version
        )

        changelog_text = QLabel(
            "• 完成桌面客户端基础 UI\n"
            "• 支持 DeepSeek / OpenRouter AI 服务\n"
            "• 支持 API Key 本地保存\n"
            "• 支持多会话聊天记录\n"
            "• 支持图片、视频和文件上传\n"
            "• 增加语音转文字输入\n"
            "• 增加自定义表情输入\n"
            "• 增加桌面透明背景模式\n"
            "• 增加用户头像本地自定义\n"
            "• 增加用户 / Arona 气泡个性化\n"
            "• 增加聊天背景自定义\n"
            "• 建立本地 workspace 工作空间\n"
            "• 优化窗口透明与 Windows 10 兼容性\n"
            "• 优化设置界面文字可读性"
        )

        changelog_text.setObjectName(
            "changelogText"
        )

        changelog_text.setWordWrap(
            True
        )

        changelog_layout.addWidget(
            changelog_text
        )

        content_layout.addWidget(
            changelog_card
        )

        # ----------------------------------------------------
        # 状态
        # ----------------------------------------------------

        status_row = QHBoxLayout()

        self.status = QLabel(
            ""
        )

        self.status.setObjectName(
            "settingsStatus"
        )

        self.status.setWordWrap(
            True
        )

        self.status.setStyleSheet(
            "color: #E5E7EB; font-size: 13px;"
        )

        test_button = QPushButton(
            "测试连接"
        )

        test_button.clicked.connect(
            self.test_connection
        )

        status_row.addWidget(
            self.status,
            1,
        )

        status_row.addWidget(
            test_button
        )

        content_layout.addLayout(
            status_row
        )

        # ----------------------------------------------------
        # 底部按钮
        #
        # 这里原本直接创建「取消 / 保存」并加到 content_layout，
        # 也就是放进了滚动区域内部。
        #
        # 结果是设置项一多，按钮就被顶到整页最下面，
        # 必须一路滚到底才能点到；
        # 窗口较小时按钮还会被裁切。
        #
        # 现在这两个按钮改由 _build_dialog_buttons() 创建，
        # 由 __init__ 固定在滚动区域之外的对话框底部。
        # 功能、文案、顺序、样式都没有改变。
        # ----------------------------------------------------

        QTimer.singleShot(
            0,
            self.refresh_profile_preview,
        )

        QTimer.singleShot(
            0,
            self.refresh_background_preview,
        )

        return content

    # ========================================================
    # 用户头像
    # ========================================================

    def choose_avatar(
        self,
    ):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择用户头像",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp)",
        )

        if not path:

            return

        try:

            source = Path(
                path
            )

            suffix = (
                source.suffix.lower()
            )

            if suffix not in (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            ):

                QMessageBox.warning(
                    self,
                    "无法使用",
                    "请选择 PNG、JPG、JPEG 或 WEBP 图片。",
                )

                return

            ensure_local_directories()

            target = (
                USER_DIR
                / f"avatar{suffix}"
            )

            # 先复制新头像，成功之后再删除旧头像。
            #
            # 原来是先删旧的再复制新的，
            # 万一复制失败（文件被占用、磁盘写满等），
            # 用户会连原来那张头像一起丢掉。
            shutil.copy2(
                source,
                target,
            )

            # 删除其它后缀的旧头像，避免多个 avatar.* 同时存在。
            for old_suffix in (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            ):

                old_path = (
                    USER_DIR
                    / f"avatar{old_suffix}"
                )

                if (
                    old_path.exists()
                    and old_path != target
                ):

                    try:

                        old_path.unlink()

                    except Exception:

                        pass

            self.selected_avatar_path = str(
                target
            )

            self.refresh_profile_preview()

            self._sync_avatar_to_main_window()

        except Exception as exc:

            QMessageBox.critical(
                self,
                "头像保存失败",
                str(exc),
            )

    def reset_avatar(
        self,
    ):

        for suffix in (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        ):

            path = (
                USER_DIR
                / f"avatar{suffix}"
            )

            if path.exists():

                try:

                    path.unlink()

                except Exception:

                    pass

        self.selected_avatar_path = ""

        self.refresh_profile_preview()

        self._sync_avatar_to_main_window()

    def _sync_avatar_to_main_window(
        self,
    ):
        """
        选好头像或恢复默认之后，
        立刻把主窗口里的头像一起更新掉。

        以前只有点了「保存」才会更新，
        现在选完图片马上就能在外面看到效果。

        这里全部包在 try 里：
        主窗口那边出任何问题，
        都不会影响设置窗口继续使用。
        """

        parent = self.parent()

        if parent is None:

            return

        try:

            if hasattr(
                parent,
                "user_avatar_path",
            ):

                parent.user_avatar_path = (
                    self._get_existing_avatar_path()
                )

            if hasattr(
                parent,
                "refresh_avatar_widgets",
            ):

                parent.refresh_avatar_widgets()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def refresh_profile_preview(
        self,
    ):

        if not hasattr(
            self,
            "avatar_preview",
        ):

            return

        path = (
            self.selected_avatar_path
            or self._get_existing_avatar_path()
        )

        if path and Path(path).exists():

            pixmap = QPixmap(
                path
            )

            if not pixmap.isNull():

                # 使用预览框的实际尺寸（84x84），
                # 而不是原来写死的 76x76，
                # 这样图片才能真正铺满整个圆形预览框。
                preview_width = (
                    self.avatar_preview.width()
                    or 84
                )

                preview_height = (
                    self.avatar_preview.height()
                    or 84
                )

                self.avatar_preview.setPixmap(
                    make_circular_pixmap(
                        scale_pixmap_to_fill(
                            pixmap,
                            preview_width,
                            preview_height,
                        )
                    )
                )

                self.avatar_preview.setText(
                    ""
                )

                return

        self.avatar_preview.clear()

        self.avatar_preview.setText(
            "●"
        )

    # ========================================================
    # 聊天背景
    # ========================================================

    def choose_background_color(
        self,
    ):

        current = QColor(
            self.appearance_settings[
                "chat_background_color"
            ]
        )

        color = QColorDialog.getColor(
            current,
            self,
            "选择聊天背景颜色",
        )

        if not color.isValid():

            return

        value = color.name(
            QColor.HexRgb
        )

        self.appearance_settings[
            "chat_background_color"
        ] = value

        self.background_color_button.setText(
            value
        )

        self._set_color_button(
            self.background_color_button,
            value,
        )

    def choose_background_image(
        self,
    ):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择聊天背景",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )

        if not path:

            return

        try:

            source = Path(
                path
            )

            suffix = (
                source.suffix.lower()
            )

            if suffix not in (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".bmp",
            ):

                QMessageBox.warning(
                    self,
                    "无法使用",
                    "请选择有效的图片文件。",
                )

                return

            ensure_local_directories()

            # 统一保存为 chat_background + 原扩展名。
            for old_suffix in (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".bmp",
            ):

                old_path = (
                    APPEARANCE_DIR
                    / f"chat_background{old_suffix}"
                )

                if old_path.exists():

                    try:

                        old_path.unlink()

                    except Exception:

                        pass

            target = (
                APPEARANCE_DIR
                / f"chat_background{suffix}"
            )

            shutil.copy2(
                source,
                target,
            )

            self.selected_background_path = str(
                target
            )

            self.appearance_settings[
                "chat_background_image"
            ] = str(
                target
            )

            self.refresh_background_preview()

            index = (
                self.chat_background_mode.findData(
                    "image"
                )
            )

            if index >= 0:

                self.chat_background_mode.setCurrentIndex(
                    index
                )

        except Exception as exc:

            QMessageBox.critical(
                self,
                "背景保存失败",
                str(exc),
            )

    def clear_background_image(
        self,
    ):

        for suffix in (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".bmp",
        ):

            path = (
                APPEARANCE_DIR
                / f"chat_background{suffix}"
            )

            if path.exists():

                try:

                    path.unlink()

                except Exception:

                    pass

        self.selected_background_path = ""

        self.appearance_settings[
            "chat_background_image"
        ] = ""

        self.refresh_background_preview()

        index = (
            self.chat_background_mode.findData(
                "default"
            )
        )

        if index >= 0:

            self.chat_background_mode.setCurrentIndex(
                index
            )

    def refresh_background_preview(
        self,
    ):

        if not hasattr(
            self,
            "background_image_label",
        ):

            return

        path = (
            self.selected_background_path
            or self.appearance_settings.get(
                "chat_background_image",
                "",
            )
        )

        if path and Path(path).exists():

            self.background_image_label.setText(
                Path(path).name
            )

        else:

            self.background_image_label.setText(
                "未选择图片"
            )

    def on_background_mode_changed(
        self,
    ):

        mode = (
            self.chat_background_mode.currentData()
        )

        if mode == "image":

            image_path = (
                self.selected_background_path
                or self.appearance_settings.get(
                    "chat_background_image",
                    "",
                )
            )

            if not image_path or not Path(
                image_path
            ).exists():

                self.status.setText(
                    "请选择一张聊天背景图片。"
                )

    # ========================================================
    # 打开本地工作空间
    # ========================================================

    def _open_directory(
        self,
        directory,
    ):

        ensure_local_directories()

        directory = Path(
            directory
        )

        try:

            if sys.platform == "win32":

                import os

                os.startfile(
                    str(directory)
                )

            elif sys.platform == "darwin":

                import subprocess

                subprocess.Popen(
                    [
                        "open",
                        str(directory),
                    ]
                )

            else:

                import subprocess

                subprocess.Popen(
                    [
                        "xdg-open",
                        str(directory),
                    ]
                )

        except Exception as exc:

            QMessageBox.warning(
                self,
                "无法打开文件夹",
                str(exc),
            )

    def open_workspace(
        self,
    ):

        self._open_directory(
            WORKSPACE_DIR
        )

    def open_results(
        self,
    ):

        self._open_directory(
            RESULTS_DIR
        )

    # ========================================================
    # 鼠标滚轮
    # ========================================================

    def wheelEvent(
        self,
        event,
    ):

        delta = event.angleDelta().y()

        if (
            delta != 0
            and hasattr(
                self,
                "scroll_area",
            )
        ):

            bar = (
                self.scroll_area.verticalScrollBar()
            )

            step = max(
                1,
                int(
                    abs(delta)
                    / 120
                ),
            )

            if delta > 0:

                bar.setValue(
                    bar.value()
                    - bar.singleStep()
                    * step
                    * 3
                )

            else:

                bar.setValue(
                    bar.value()
                    + bar.singleStep()
                    * step
                    * 3
                )

            event.accept()

            return

        super().wheelEvent(
            event
        )

    # ========================================================
    # 当前设置
    # ========================================================

    def load_current(
        self,
    ):

        current_provider = (
            self.settings.get_provider()
        )

        index = self.provider.findData(
            current_provider
        )

        if index >= 0:

            self.provider.setCurrentIndex(
                index
            )

        self.refresh_provider_fields()

        self.refresh_profile_preview()

        self.refresh_background_preview()

    def on_provider_changed(
        self,
    ):

        self.refresh_provider_fields()

    def refresh_provider_fields(
        self,
    ):

        provider = (
            self.provider.currentData()
        )

        self.model.setText(
            self.settings.get_model(
                provider
            )
        )

        existing_key = (
            self.settings.get_api_key(
                provider
            )
        )

        self.api_key.setText(
            existing_key
        )

        display_name = (
            "DeepSeek"
            if provider == "deepseek"
            else "OpenRouter"
        )

        self.status.setText(
            f"当前编辑：{display_name} · "
            + (
                "已配置 API Key"
                if existing_key
                else "尚未配置 API Key"
            )
        )

    # ========================================================
    # 测试连接
    # ========================================================

    def test_connection(
        self,
    ):

        provider = (
            self.provider.currentData()
        )

        model = (
            self.model.text().strip()
        )

        api_key = (
            self.api_key.text().strip()
        )

        if not model:

            QMessageBox.warning(
                self,
                "无法测试",
                "模型名称不能为空。",
            )

            return

        if not api_key:

            QMessageBox.warning(
                self,
                "无法测试",
                "请先输入 API Key。",
            )

            return

        self.status.setText(
            "正在测试连接……"
        )

        QApplication.processEvents()

        try:

            result = (
                self.ai_client.test_connection(
                    provider=provider,
                    model=model,
                    api_key=api_key,
                )
            )

            self.status.setText(
                f"连接成功：{result}"
            )

        except Exception as exc:

            self.status.setText(
                f"连接失败：{exc}"
            )

    # ========================================================
    # 保存设置
    # ========================================================

    def save_settings(
        self,
    ):

        provider = (
            self.provider.currentData()
        )

        model = (
            self.model.text().strip()
        )

        api_key = (
            self.api_key.text().strip()
        )

        if not model:

            QMessageBox.warning(
                self,
                "无法保存",
                "模型名称不能为空。",
            )

            return

        try:

            self.settings.save(
                provider,
                model,
                api_key,
            )

            self.ai_client.reload()

            # -----------------------------------------------
            # 保存第一阶段个性化设置
            # -----------------------------------------------

            appearance = (
                self._normalize_appearance_settings(
                    dict(
                        self.appearance_settings
                    )
                )
            )

            appearance[
                "bubble_opacity"
            ] = self.bubble_opacity_slider.value()

            appearance[
                "bubble_radius"
            ] = self.bubble_radius_slider.value()

            appearance[
                "chat_background_opacity"
            ] = self.background_opacity_slider.value()

            appearance[
                "chat_background_mode"
            ] = self.chat_background_mode.currentData()

            appearance[
                "chat_background_image"
            ] = (
                self.selected_background_path
                or ""
            )

            if (
                appearance[
                    "chat_background_mode"
                ] == "image"
                and (
                    not appearance[
                        "chat_background_image"
                    ]
                    or not Path(
                        appearance[
                            "chat_background_image"
                        ]
                    ).exists()
                )
            ):

                appearance[
                    "chat_background_mode"
                ] = "default"

            # 保存桌面透明背景开关和透明度。
            #
            # 以前这两项只发给了主窗口，没有写进 appearance，
            # 所以设置完一重启就回到默认值。
            appearance[
                "desktop_transparent"
            ] = bool(
                self.desktop_transparent_checkbox.isChecked()
            )

            appearance[
                "desktop_background_alpha"
            ] = int(
                self.opacity_slider.value()
                * 255
                / 100
            )

            save_appearance_settings(
                appearance
            )

            # 桌面感知开关：独立保存，与外观设置无关。
            #
            # 关闭时立刻生效 —— 判据每次调用都会重新读设置，
            # 所以保存后后续的截图请求会立即被拒绝。
            _new_dv_enabled = self.desktop_vision_checkbox.isChecked()
            self.settings.set_desktop_vision_enabled(_new_dv_enabled)

            # 若用户关闭 DV 开关，立即停止正在运行的感知引擎；
            # 否则感知引擎每轮截图都会被门禁拒绝，但仍会无意义地轮询。
            if (
                not _new_dv_enabled
                and self.desktop_awareness is not None
                and self.desktop_awareness.is_running
            ):
                self._stop_desktop_awareness()

            # V0.3 保存桌面自主感知设置
            if hasattr(self, "awareness_enabled_checkbox"):
                _aw_settings: dict = self.settings.settings.setdefault(
                    "desktop_awareness", {}
                )
                _aw_settings["enabled"] = (
                    self.awareness_enabled_checkbox.isChecked()
                )
                _aw_settings["interval_seconds"] = (
                    self.awareness_interval_spin.value()
                )
                self.settings._save_settings()

            self.saved.emit()

            self.appearance_saved.emit(
                self.desktop_transparent_checkbox.isChecked(),
                int(
                    self.opacity_slider.value()
                    * 255
                    / 100
                ),
            )

            self.accept()

        except Exception as exc:

            QMessageBox.critical(
                self,
                "保存失败",
                str(exc),
            )


# ============================================================
# 桌面截图确认对话框（V0.2 第一阶段）
# ============================================================

class DesktopCaptureDialog(QDialog):
    """
    把桌面截图发送给 AI 之前，先让用户看清楚并确认。

    对话框里明确写出：
      * 这次要发送的截图（缩略图预览）
      * 截图原始尺寸、压缩后尺寸、JPEG 大小
      * 将要发送给哪个服务商、哪个模型
      * 当前模型是否确认支持图像输入

    只有用户点「发送分析」才会真正上传。
    """

    def __init__(
        self,
        parent,
        shot,
        support,
        provider,
    ):
        super().__init__(parent)

        self.shot = shot
        self.support = support

        self.setWindowTitle("桌面截图")
        self.setMinimumWidth(560)

        self.setStyleSheet(
            """
            QDialog {
                background: #000000;
                color: #F5F7FA;
            }

            QLabel {
                color: #F5F7FA;
            }

            #desktopPreview {
                background: #111111;
                border: 1px solid #353535;
                border-radius: 10px;
                padding: 6px;
            }

            #desktopNotice {
                color: #C9D1DC;
            }

            #desktopWarning {
                color: #FFB4B4;
            }

            QTextEdit {
                background: #111111;
                border: 1px solid #353535;
                border-radius: 8px;
                padding: 8px;
                color: #FFFFFF;
            }

            QPushButton {
                background: #151515;
                border: 1px solid #353535;
                border-radius: 8px;
                padding: 8px 16px;
                color: #F5F7FA;
            }

            QPushButton:hover {
                border: 1px solid #4A8DF4;
            }

            QPushButton#primaryButton {
                background: #2D6FE2;
                border: none;
                color: #FFFFFF;
                font-weight: 700;
            }

            QPushButton:disabled {
                color: #707070;
                border: 1px solid #252525;
            }
            """
        )

        root = QVBoxLayout(
            self
        )

        root.setContentsMargins(
            24,
            20,
            24,
            20,
        )

        root.setSpacing(
            12
        )

        headline = QLabel(
            "发送桌面截图给 AI 分析"
        )

        root.addWidget(
            headline
        )

        # ----------------------------------------------------
        # 预览
        # ----------------------------------------------------

        preview = QLabel()

        preview.setObjectName(
            "desktopPreview"
        )

        preview.setAlignment(
            Qt.AlignCenter
        )

        pixmap = QPixmap()

        if shot.image is not None:

            pixmap = QPixmap.fromImage(
                shot.image
            )

        if not pixmap.isNull():

            preview.setPixmap(
                pixmap.scaled(
                    500,
                    282,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

        else:

            preview.setText(
                "（没有可用的预览图）"
            )

        root.addWidget(
            preview
        )

        # ----------------------------------------------------
        # 说明
        # ----------------------------------------------------

        info = QLabel(
            shot.summary()
            + (
                "\n（截图前已暂时隐藏 AronaClient 窗口，"
                "所以画面里不会包含本程序自己）"
                if getattr(shot, "self_hidden", False)
                else ""
            )
        )

        info.setObjectName(
            "desktopNotice"
        )

        info.setWordWrap(
            True
        )

        root.addWidget(
            info
        )

        destination = QLabel(
            f"将要发送给："
            f"{provider or '未知服务商'} · {support.model or '未知模型'}"
        )

        destination.setObjectName(
            "desktopNotice"
        )

        destination.setWordWrap(
            True
        )

        root.addWidget(
            destination
        )

        # ----------------------------------------------------
        # 视觉能力 / 提问
        # ----------------------------------------------------

        if support.supported:

            self.question = QTextEdit()

            self.question.setPlaceholderText(
                "想问什么？留空就让 Arona 直接描述整张截图。"
            )

            self.question.setFixedHeight(
                72
            )

            root.addWidget(
                self.question
            )

            if not support.confirmed:

                caution = QLabel(
                    support.reason
                )

                caution.setObjectName(
                    "desktopNotice"
                )

                caution.setWordWrap(
                    True
                )

                root.addWidget(
                    caution
                )

        else:

            self.question = None

            warning = QLabel(
                "当前模型无法分析图片，截图不会被发送。\n"
                f"原因：{support.reason}"
            )

            warning.setObjectName(
                "desktopWarning"
            )

            warning.setWordWrap(
                True
            )

            root.addWidget(
                warning
            )

        # ----------------------------------------------------
        # 按钮
        # ----------------------------------------------------

        buttons = QHBoxLayout()

        buttons.addStretch()

        cancel_button = QPushButton(
            "取消"
        )

        cancel_button.clicked.connect(
            self.reject
        )

        send_button = QPushButton(
            "发送分析"
        )

        send_button.setObjectName(
            "primaryButton"
        )

        send_button.setEnabled(
            bool(support.supported)
        )

        send_button.clicked.connect(
            self.accept
        )

        buttons.addWidget(
            cancel_button
        )

        buttons.addWidget(
            send_button
        )

        root.addLayout(
            buttons
        )

    def get_question(
        self,
    ):
        """取得用户填写的提问，未填写时返回空字符串。"""

        if self.question is None:

            return ""

        return (
            self.question.toPlainText()
            or ""
        ).strip()


# ============================================================
# 主界面
# ============================================================

class AronaUI(QMainWindow):

    def __init__(
        self,
    ):

        super().__init__()

        ensure_local_directories()

        self.ai_client = AIClient()

        self.sidebar_visible = True
        self.desktop_transparent = False

        self.desktop_background_alpha = 235

        # 启动时只需要恢复一次桌面透明设置。
        self._desktop_transparency_restored = False

        # ----------------------------------------------------
        # 第一阶段：个性化设置
        # ----------------------------------------------------

        self.appearance_settings = (
            load_appearance_settings()
        )

        self.appearance_settings = (
            self._normalize_appearance_settings()
        )

        # 恢复上次保存的桌面透明背景设置。
        #
        # 这两项以前没有保存，所以每次启动都会回到
        # 「关闭 + 235」，用户的设置等于白调。
        self.desktop_transparent = bool(
            self.appearance_settings.get(
                "desktop_transparent",
                False,
            )
        )

        self.desktop_background_alpha = clamp_int(
            self.appearance_settings.get(
                "desktop_background_alpha",
                235,
            ),
            0,
            255,
            235,
        )

        self.user_avatar_path = (
            self._get_existing_avatar_path()
        )

        # 必须在 QMainWindow 第一次 resizeEvent 发生之前创建。
        self.desktop_background_pixmap = QPixmap()
        self.desktop_background_label = None

        # 自定义聊天背景图片。
        #
        # 这里只保存「原图」，不预先缩放。
        # 每次重绘时再按聊天区域的实际大小等比裁切，
        # 这样窗口大小改变时背景也能跟着正确适配。
        self.chat_background_pixmap = QPixmap()

        # 缓存标识：图片路径 + 修改时间 + 大小。
        # 用户在设置里换一张同后缀的图片时路径不变，
        # 靠修改时间才能知道需要重新读取。
        self.chat_background_key = ""

        # 按当前尺寸裁切好的成图缓存。
        # viewport 每次重绘都要用，
        # 缓存起来就不必反复缩放同一张大图。
        self.chat_background_scaled = QPixmap()

        self.chat_background_scaled_key = None

        # ----------------------------------------------------
        # 窗口透明属性：只在窗口显示之前设置一次，之后永不改动。
        #
        # Qt 要求 WA_TranslucentBackground 必须在窗口创建 /
        # 显示之前设定。窗口已经显示之后再改这个属性，
        # Qt 不会重建原生窗口，在部分 Windows 显卡驱动上
        # 会出现重绘不完整、局部空白，看起来像「控件消失」。
        #
        # 所以这里恢复成和原版一致：无条件设为 True，
        # 之后任何地方都不再改动它。
        # 「桌面透明背景」开关只通过样式表、调色板和
        # _set_windows_desktop_passthrough 体现，
        # 不依赖这个属性。
        # ----------------------------------------------------

        self.setAttribute(
            Qt.WA_TranslucentBackground,
            True,
        )

        self.setAttribute(
            Qt.WA_NoSystemBackground,
            True,
        )

        self.setAutoFillBackground(
            False
        )

        self.worker_threads = []

        self.attachments = []

        # ── V0.3 桌面自主感知 ────────────────────────────────
        self.desktop_awareness = None  # type: ignore[assignment]
        self._awareness_active = False
        self._awareness_init_done = False
        self._desktop_vision_busy = False  # 防止重复触发截图分析任务
        self._awareness_reply_busy = False  # 防止重复提交 AI 回复请求
        self.awareness_reply_threads = []  # 追踪感知回复线程

        # ── R12.3-07 M3：图片生成 ──
        self.image_gen_threads = []      # 追踪图片生成线程 (thread, worker)
        self._image_gen_busy = False     # 防止重复提交
        self._image_gen_service_obj = None  # 惰性构造（门禁不满足时不建网络）

        self.speech_thread = None
        self.speech_worker = None

        self._refreshing_history = False

        self._ai_busy = False

        # ---- 桌面小人物（V0.1）----
        #
        # 只在最小化时创建；同一时间最多一个实例。
        self.mascot_window = None

        self._mascot_busy = False

        self._mascot_shutting_down = False

        self._thread_shutdown_report = []


        self.setWindowTitle(
            "AronaClient"
        )

        self.resize(
            1440,
            900,
        )

        self.setMinimumSize(
            1050,
            700,
        )

        self.build_ui()

        self.apply_style()

        self.refresh_model_card()

        # ── V0.3 桌面自主感知初始化 ───────────────────────────
        self._init_desktop_awareness()

        self.history_list.itemClicked.connect(
            self.on_history_item_clicked
        )

        self.refresh_history_list()

        self.restore_current_conversation_to_ui()

    # ========================================================
    # 个性化设置辅助
    # ========================================================

    def _normalize_appearance_settings(
        self,
    ):

        settings = dict(
            DEFAULT_APPEARANCE
        )

        settings.update(
            getattr(
                self,
                "appearance_settings",
                {},
            )
        )

        settings[
            "user_bubble_color"
        ] = normalize_hex_color(
            settings.get(
                "user_bubble_color"
            ),
            DEFAULT_APPEARANCE[
                "user_bubble_color"
            ],
        )

        settings[
            "ai_bubble_color"
        ] = normalize_hex_color(
            settings.get(
                "ai_bubble_color"
            ),
            DEFAULT_APPEARANCE[
                "ai_bubble_color"
            ],
        )

        settings[
            "message_text_color"
        ] = normalize_hex_color(
            settings.get(
                "message_text_color"
            ),
            DEFAULT_APPEARANCE[
                "message_text_color"
            ],
        )

        settings[
            "user_text_color"
        ] = normalize_hex_color(
            settings.get(
                "user_text_color"
            ),
            DEFAULT_APPEARANCE[
                "user_text_color"
            ],
        )

        settings[
            "ai_text_color"
        ] = normalize_hex_color(
            settings.get(
                "ai_text_color"
            ),
            DEFAULT_APPEARANCE[
                "ai_text_color"
            ],
        )

        settings[
            "chat_background_color"
        ] = normalize_hex_color(
            settings.get(
                "chat_background_color"
            ),
            DEFAULT_APPEARANCE[
                "chat_background_color"
            ],
        )

        settings[
            "bubble_opacity"
        ] = clamp_int(
            settings.get(
                "bubble_opacity",
                98,
            ),
            20,
            100,
            98,
        )

        settings[
            "bubble_radius"
        ] = clamp_int(
            settings.get(
                "bubble_radius",
                14,
            ),
            0,
            40,
            14,
        )

        settings[
            "chat_background_opacity"
        ] = clamp_int(
            settings.get(
                "chat_background_opacity",
                100,
            ),
            0,
            100,
            100,
        )

        if settings.get(
            "chat_background_mode"
        ) not in (
            "default",
            "color",
            "image",
        ):

            settings[
                "chat_background_mode"
            ] = "default"

        return settings

    def _get_existing_avatar_path(
        self,
    ):

        for suffix in (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        ):

            path = (
                USER_DIR
                / f"avatar{suffix}"
            )

            if path.exists():

                return str(
                    path
                )

        return ""

    def _avatar_pixmap(
        self,
        path,
        size,
    ):

        if not path:

            return QPixmap()

        try:

            pixmap = QPixmap(
                path
            )

            if pixmap.isNull():

                return QPixmap()

            # 等比铺满整个头像区域，再裁成圆形。
            #
            # 原来这里用的是 Qt.KeepAspectRatio，
            # 横图会被缩成 size x (size/2)，
            # 头像里会空掉一半，所以改成铺满 + 裁圆。
            return make_circular_pixmap(
                scale_pixmap_to_fill(
                    pixmap,
                    size,
                    size,
                )
            )

        except Exception:

            return QPixmap()

    def _apply_avatar_to_label(
        self,
        label,
        path,
        fallback,
        size,
    ):

        pixmap = self._avatar_pixmap(
            path,
            size,
        )

        if not pixmap.isNull():

            label.setPixmap(
                pixmap
            )

            label.setText(
                ""
            )

        else:

            label.setPixmap(
                QPixmap()
            )

            label.setText(
                fallback
            )

    # ========================================================
    # UI
    # ========================================================

    def build_ui(
        self,
    ):

        central = QWidget()

        central.setObjectName(
            "central"
        )

        central.setAttribute(
            Qt.WA_TranslucentBackground,
            True,
        )

        central.setAutoFillBackground(
            False
        )

        self.setCentralWidget(
            central
        )

        self.desktop_background_label = QLabel(
            central
        )

        self.desktop_background_label.setObjectName(
            "desktopBackground"
        )

        self.desktop_background_label.setScaledContents(
            True
        )

        self.desktop_background_label.lower()
        self.desktop_background_label.hide()

        root = QHBoxLayout(
            central
        )

        root.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        root.setSpacing(
            0
        )

        self.sidebar = (
            self.build_sidebar()
        )

        root.addWidget(
            self.sidebar
        )

        main = QWidget()

        main.setObjectName(
            "mainArea"
        )

        main.setAttribute(
            Qt.WA_TranslucentBackground,
            True,
        )

        main.setAutoFillBackground(
            False
        )

        main_layout = QVBoxLayout(
            main
        )

        main_layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        main_layout.setSpacing(
            0
        )

        # ----------------------------------------------------
        # 顶栏
        # ----------------------------------------------------

        topbar = QFrame()

        topbar.setObjectName(
            "topbar"
        )

        top_layout = QHBoxLayout(
            topbar
        )

        top_layout.setContentsMargins(
            22,
            12,
            22,
            12,
        )

        self.sidebar_toggle = QToolButton()

        self.sidebar_toggle.setText(
            "☰"
        )

        self.sidebar_toggle.setObjectName(
            "sidebarToggle"
        )

        self.sidebar_toggle.setToolTip(
            "隐藏侧边栏"
        )

        self.sidebar_toggle.clicked.connect(
            self.toggle_sidebar
        )

        top_layout.addWidget(
            self.sidebar_toggle
        )

        self.avatar = QLabel(
            "A"
        )

        self.avatar.setObjectName(
            "topAvatar"
        )

        self.avatar.setAlignment(
            Qt.AlignCenter
        )

        self._apply_avatar_to_label(
            self.avatar,
            "",
            "A",
            38,
        )

        name_box = QVBoxLayout()

        name_box.setSpacing(
            0
        )

        name = QLabel(
            "Arona"
        )

        name.setObjectName(
            "topName"
        )

        provider = QLabel(
            "AI Assistant"
        )

        provider.setObjectName(
            "topSubtitle"
        )

        name_box.addWidget(
            name
        )

        name_box.addWidget(
            provider
        )

        top_layout.addWidget(
            self.avatar
        )

        top_layout.addLayout(
            name_box
        )

        top_layout.addStretch()

        self.model_toggle = QToolButton()

        self.model_toggle.setText(
            "◉  3D 模型"
        )

        self.model_toggle.setCheckable(
            True
        )

        self.model_toggle.setChecked(
            True
        )

        self.model_toggle.setToolTip(
            "显示 / 隐藏 3D 模型区域"
        )

        self.model_toggle.clicked.connect(
            self.toggle_model_stage
        )

        fullscreen_button = QToolButton()

        fullscreen_button.setText(
            "⛶"
        )

        fullscreen_button.setToolTip(
            "模型全屏"
        )

        fullscreen_button.clicked.connect(
            self.fullscreen_model_stage
        )

        settings_button = QToolButton()

        settings_button.setText(
            "⚙"
        )

        settings_button.setToolTip(
            "设置"
        )

        settings_button.clicked.connect(
            self.open_settings
        )

        # R12.3-07 M3：图片生成入口（默认关闭；门禁不满足时拒绝，零网络）。
        image_gen_button = QToolButton()

        image_gen_button.setText(
            "🖼"
        )

        image_gen_button.setToolTip(
            "生成图片（需在设置中开启并配置 API Key）"
        )

        image_gen_button.clicked.connect(
            self.start_image_generation
        )

        top_layout.addWidget(
            self.model_toggle
        )

        top_layout.addWidget(
            fullscreen_button
        )

        top_layout.addWidget(
            image_gen_button
        )

        top_layout.addWidget(
            settings_button
        )

        main_layout.addWidget(
            topbar
        )

        # ----------------------------------------------------
        # 3D 模型舞台
        # ----------------------------------------------------

        self.model_stage = (
            self.build_model_stage()
        )

        main_layout.addWidget(
            self.model_stage
        )

        # ----------------------------------------------------
        # 聊天滚动区
        # ----------------------------------------------------

        self.chat_scroll = QScrollArea()

        self.chat_scroll.setObjectName(
            "chatScroll"
        )

        self.chat_scroll.setWidgetResizable(
            True
        )

        self.chat_scroll.setFrameShape(
            QFrame.NoFrame
        )

        self.chat_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )

        self.chat_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded
        )

        self.chat_container = QWidget()

        self.chat_container.setObjectName(
            "chatContainer"
        )

        self.chat_layout = QVBoxLayout(
            self.chat_container
        )

        self.chat_layout.setContentsMargins(
            32,
            28,
            32,
            20,
        )

        self.chat_layout.setSpacing(
            18
        )

        self.chat_layout.addStretch()

        self.chat_scroll.setWidget(
            self.chat_container
        )

        chat_viewport = (
            self.chat_scroll.viewport()
        )

        for widget in (
            self.chat_scroll,
            chat_viewport,
            self.chat_container,
        ):

            widget.setAttribute(
                Qt.WA_OpaquePaintEvent,
                False,
            )

            widget.setAttribute(
                Qt.WA_NoSystemBackground,
                True,
            )

            widget.setAutoFillBackground(
                False
            )

        # ----------------------------------------------------
        # 自定义聊天背景
        #
        # 背景图片要自己画，不能交给样式表：
        #
        # 1. Qt 不支持 QAbstractScrollArea::viewport 这个选择器，
        #    整条规则会被直接忽略，
        #    所以原来「选了背景图却什么都不显示」。
        #
        # 2. Qt 样式表也不支持 background-size，
        #    即使能显示，也只会按图片原始像素画在中间，
        #    既铺不满，也不会随窗口大小变化。
        #
        # 这里给 viewport 安装事件过滤器，
        # 在重绘和尺寸变化时按当前大小重新裁切绘制。
        # 原来的布局和滚动结构完全没有改动。
        # ----------------------------------------------------

        chat_viewport.installEventFilter(
            self
        )

        main_layout.addWidget(
            self.chat_scroll,
            1,
        )

        composer = (
            self.build_composer()
        )

        main_layout.addWidget(
            composer
        )

        root.addWidget(
            main,
            1,
        )

    # ========================================================
    # Sidebar
    # ========================================================

    def build_sidebar(
        self,
    ):

        sidebar = QFrame()

        sidebar.setObjectName(
            "sidebar"
        )

        sidebar.setFixedWidth(
            270
        )

        layout = QVBoxLayout(
            sidebar
        )

        layout.setContentsMargins(
            16,
            18,
            16,
            16,
        )

        layout.setSpacing(
            12
        )

        title_row = QHBoxLayout()

        logo = QLabel(
            "◉"
        )

        logo.setObjectName(
            "logo"
        )

        title = QLabel(
            "AronaClient"
        )

        title.setObjectName(
            "appTitle"
        )

        menu = QToolButton()

        menu.setText(
            "☰"
        )

        menu.setToolTip(
            "收起侧边栏"
        )

        menu.clicked.connect(
            self.toggle_sidebar
        )

        title_row.addWidget(
            logo
        )

        title_row.addWidget(
            title
        )

        title_row.addStretch()

        title_row.addWidget(
            menu
        )

        layout.addLayout(
            title_row
        )

        new_chat = QPushButton(
            "＋  新建对话"
        )

        new_chat.setObjectName(
            "newChatButton"
        )

        new_chat.clicked.connect(
            self.new_chat
        )

        layout.addWidget(
            new_chat
        )

        plugin_title = QLabel(
            "功能插件"
        )

        plugin_title.setObjectName(
            "sectionTitle"
        )

        layout.addWidget(
            plugin_title
        )

        # V0.2 第一阶段：
        # 每一项可以带一个回调；没有回调的保持原来的「即将上线」禁用状态。
        desktop_vision_ready = (
            DesktopVision is not None
        )

        plugins = [
            ("◎  浏览器", "即将上线", None),
            ("▣  文件", "即将上线", None),
            ("▧  图片", "即将上线", None),
            ("▶  视频", "即将上线", None),
            ("♩  语音", "即将上线", None),
            (
                "▤  桌面截图",
                "可用" if desktop_vision_ready else "不可用",
                (
                    self.capture_desktop_for_ai
                    if desktop_vision_ready
                    else None
                ),
            ),
        ]

        for text, badge, handler in plugins:

            row = QHBoxLayout()

            button = QToolButton()

            button.setText(
                text
            )

            button.setToolButtonStyle(
                Qt.ToolButtonTextBesideIcon
            )

            # 有回调才可点；其余仍然禁用。
            button.setEnabled(
                handler is not None
            )

            if handler is not None:

                button.clicked.connect(
                    handler
                )

            label = QLabel(
                badge
            )

            label.setObjectName(
                "pluginBadge"
            )

            row.addWidget(
                button
            )

            row.addStretch()

            row.addWidget(
                label
            )

            layout.addLayout(
                row
            )

        line1 = QFrame()

        line1.setFrameShape(
            QFrame.HLine
        )

        line1.setObjectName(
            "sidebarLine"
        )

        layout.addWidget(
            line1
        )

        history_title = QLabel(
            "聊天记录"
        )

        history_title.setObjectName(
            "sectionTitle"
        )

        layout.addWidget(
            history_title
        )

        self.history_list = QListWidget()

        self.history_list.setObjectName(
            "historyList"
        )

        self.history_list.setMaximumHeight(
            220
        )

        layout.addWidget(
            self.history_list
        )

        line2 = QFrame()

        line2.setFrameShape(
            QFrame.HLine
        )

        line2.setObjectName(
            "sidebarLine"
        )

        layout.addWidget(
            line2
        )

        model_title = QLabel(
            "当前模型"
        )

        model_title.setObjectName(
            "sectionTitle"
        )

        layout.addWidget(
            model_title
        )

        self.model_card = QFrame()

        self.model_card.setObjectName(
            "modelCard"
        )

        model_layout = QHBoxLayout(
            self.model_card
        )

        model_layout.setContentsMargins(
            12,
            12,
            12,
            12,
        )

        model_avatar = QLabel(
            "A"
        )

        model_avatar.setObjectName(
            "modelAvatar"
        )

        model_avatar.setAlignment(
            Qt.AlignCenter
        )

        model_text = QVBoxLayout()

        self.model_name_label = QLabel(
            "Arona"
        )

        self.model_name_label.setObjectName(
            "modelName"
        )

        self.model_info_label = QLabel(
            "DeepSeek · deepseek-chat"
        )

        self.model_info_label.setObjectName(
            "modelInfo"
        )

        self.model_status_label = QLabel(
            "● 未配置"
        )

        self.model_status_label.setObjectName(
            "modelStatus"
        )

        model_text.addWidget(
            self.model_name_label
        )

        model_text.addWidget(
            self.model_info_label
        )

        model_text.addWidget(
            self.model_status_label
        )

        model_layout.addWidget(
            model_avatar
        )

        model_layout.addLayout(
            model_text
        )

        layout.addWidget(
            self.model_card
        )

        layout.addStretch()

        settings = QPushButton(
            "⚙  设置"
        )

        settings.setObjectName(
            "sidebarSettings"
        )

        settings.clicked.connect(
            self.open_settings
        )

        layout.addWidget(
            settings
        )

        return sidebar

    # ========================================================
    # 3D 模型舞台
    # ========================================================

    def build_model_stage(
        self,
    ):

        stage = QFrame()

        stage.setObjectName(
            "modelStage"
        )

        stage.setMinimumHeight(
            250
        )

        stage.setMaximumHeight(
            390
        )

        layout = QVBoxLayout(
            stage
        )

        layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        self.model_placeholder = QWidget()

        self.model_placeholder.setObjectName(
            "modelPlaceholder"
        )

        placeholder_layout = QVBoxLayout(
            self.model_placeholder
        )

        placeholder_layout.setAlignment(
            Qt.AlignCenter
        )

        halo = QLabel(
            "ARONA"
        )

        halo.setObjectName(
            "modelPlaceholderTitle"
        )

        halo.setAlignment(
            Qt.AlignCenter
        )

        description = QLabel(
            "3D 模型舞台\n\n"
            "这里就是以后真正的 Arona 3D 模型所在的位置。\n"
            "当前版本先完成 UI、显示 / 隐藏和全屏结构。"
        )

        description.setObjectName(
            "modelPlaceholderText"
        )

        description.setAlignment(
            Qt.AlignCenter
        )

        description.setWordWrap(
            True
        )

        placeholder_layout.addWidget(
            halo
        )

        placeholder_layout.addWidget(
            description
        )

        layout.addWidget(
            self.model_placeholder
        )

        bottom_overlay = QFrame(
            stage
        )

        bottom_overlay.setObjectName(
            "stageOverlay"
        )

        bottom_overlay.setFixedHeight(
            48
        )

        overlay_layout = QHBoxLayout(
            bottom_overlay
        )

        overlay_layout.setContentsMargins(
            18,
            6,
            18,
            6,
        )

        self.stage_status = QLabel(
            "Arona 正在等待你的消息～"
        )

        self.stage_status.setObjectName(
            "stageStatus"
        )

        overlay_layout.addWidget(
            self.stage_status
        )

        overlay_layout.addStretch()

        stage_fullscreen = QToolButton()

        stage_fullscreen.setText(
            "⛶"
        )

        stage_fullscreen.clicked.connect(
            self.fullscreen_model_stage
        )

        overlay_layout.addWidget(
            stage_fullscreen
        )

        layout.addWidget(
            bottom_overlay
        )

        return stage

    # ========================================================
    # Composer
    # ========================================================

    def build_composer(
        self,
    ):

        outer = QFrame()

        outer.setObjectName(
            "composerOuter"
        )

        layout = QVBoxLayout(
            outer
        )

        layout.setContentsMargins(
            28,
            8,
            28,
            18,
        )

        layout.setSpacing(
            8
        )

        self.attachment_row = QHBoxLayout()

        self.attachment_row.setSpacing(
            6
        )

        layout.addLayout(
            self.attachment_row
        )

        composer = QFrame()

        composer.setObjectName(
            "composer"
        )

        composer_layout = QHBoxLayout(
            composer
        )

        composer_layout.setContentsMargins(
            8,
            8,
            8,
            8,
        )

        composer_layout.setSpacing(
            8
        )

        plus = QToolButton()

        plus.setText(
            "＋"
        )

        plus.setObjectName(
            "roundToolButton"
        )

        plus.setToolTip(
            "添加图片 / 视频 / 文件"
        )

        plus_menu = (
            self.build_attachment_menu(
                plus
            )
        )

        plus.setMenu(
            plus_menu
        )

        plus.setPopupMode(
            QToolButton.InstantPopup
        )

        emoji_button = QToolButton()

        emoji_button.setText(
            "😊"
        )

        emoji_button.setObjectName(
            "roundToolButton"
        )

        emoji_button.setToolTip(
            "插入表情"
        )

        emoji_menu = self.build_emoji_menu(
            emoji_button
        )

        emoji_button.setMenu(
            emoji_menu
        )

        emoji_button.setPopupMode(
            QToolButton.InstantPopup
        )

        self.input = QTextEdit()

        self.input.setObjectName(
            "messageInput"
        )

        self.input.setPlaceholderText(
            "输入消息…"
        )

        self.input.setAcceptRichText(
            False
        )

        self.input.setFixedHeight(
            52
        )

        self.input.installEventFilter(
            self
        )

        self.voice_button = QToolButton()

        self.voice_button.setText(
            "🎤"
        )

        self.voice_button.setObjectName(
            "roundToolButton"
        )

        self.voice_button.setToolTip(
            "点击进行语音输入"
        )

        self.voice_button.setEnabled(
            True
        )

        self.voice_button.clicked.connect(
            self.start_voice_input
        )

        send = QToolButton()

        send.setText(
            "➤"
        )

        send.setObjectName(
            "sendButton"
        )

        send.clicked.connect(
            self.send_message
        )

        composer_layout.addWidget(
            plus
        )

        composer_layout.addWidget(
            emoji_button
        )

        composer_layout.addWidget(
            self.input,
            1,
        )

        composer_layout.addWidget(
            self.voice_button
        )

        composer_layout.addWidget(
            send
        )

        layout.addWidget(
            composer
        )

        return outer

    def build_emoji_menu(
        self,
        parent,
    ):

        from PySide6.QtWidgets import QMenu

        menu = QMenu(
            parent
        )

        menu.setObjectName(
            "emojiMenu"
        )

        emoji_groups = [
            (
                "常用表情",
                "😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 🙃 😉 😍 🥰 😘 😋 😎 🤔 🥺 😭 😢 😤 😡",
            ),
            (
                "手势与爱心",
                "👍 👎 👌 ✌️ 🤞 👏 🙌 👋 🤝 ❤️ 🧡 💛 💚 💙 💜 🤍 💖 💕 💗",
            ),
            (
                "其他",
                "🎉 🎊 ✨ ⭐ 🌟 🔥 💯 ✅ ❌ 💡 ☀️ 🌈 ☕ 🍰 🐱 🐰 🐻",
            ),
        ]

        for group_index, (
            title,
            emojis,
        ) in enumerate(
            emoji_groups
        ):

            if group_index:

                menu.addSeparator()

            menu.addSection(
                title
            )

            for emoji in emojis.split():

                action = QAction(
                    emoji,
                    self,
                )

                action.triggered.connect(
                    lambda checked=False,
                    value=emoji:
                    self.insert_emoji(
                        value
                    )
                )

                menu.addAction(
                    action
                )

        menu.setStyleSheet(
            "QMenu { background: #121d2e; color: #eaf1fb; border: 1px solid #2b3f5f; padding: 6px; }"
            "QMenu::item { padding: 7px 9px; border-radius: 5px; }"
            "QMenu::item:selected { background: #263f62; }"
        )

        return menu

    def insert_emoji(
        self,
        emoji,
    ):

        cursor = self.input.textCursor()

        cursor.insertText(
            emoji
        )

        self.input.setTextCursor(
            cursor
        )

        self.input.setFocus()

    def build_attachment_menu(
        self,
        parent,
    ):

        from PySide6.QtWidgets import QMenu

        menu = QMenu(
            parent
        )

        image_action = QAction(
            "🖼  图片",
            self,
        )

        image_action.triggered.connect(
            self.add_image
        )

        video_action = QAction(
            "🎬  视频",
            self,
        )

        video_action.triggered.connect(
            self.add_video
        )

        file_action = QAction(
            "📄  文件",
            self,
        )

        file_action.triggered.connect(
            self.add_file
        )

        menu.addAction(
            image_action
        )

        menu.addAction(
            video_action
        )

        menu.addAction(
            file_action
        )

        return menu

    # ========================================================
    # Voice
    # ========================================================

    def start_voice_input(
        self,
    ):

        if (
            self.speech_thread is not None
            and self.speech_thread.isRunning()
        ):

            return

        self.stage_status.setText(
            "Arona 正在听你说话……"
        )

        self.voice_button.setEnabled(
            False
        )

        self.voice_button.setText(
            "⏺"
        )

        thread = QThread(
            self
        )

        worker = SpeechWorker()

        worker.moveToThread(
            thread
        )

        thread.started.connect(
            worker.run
        )

        worker.status.connect(
            self.speech_status
        )

        worker.finished.connect(
            self.speech_finished
        )

        worker.error.connect(
            self.speech_error
        )

        worker.finished.connect(
            thread.quit
        )

        worker.error.connect(
            thread.quit
        )

        worker.finished.connect(
            worker.deleteLater
        )

        worker.error.connect(
            worker.deleteLater
        )

        thread.finished.connect(
            thread.deleteLater
        )

        self.speech_thread = thread

        self.speech_worker = worker

        thread.finished.connect(
            self.speech_thread_finished
        )

        thread.start()

    def speech_status(
        self,
        status,
    ):

        if status == "__LISTENING__":

            self.stage_status.setText(
                "🎤 Arona 正在听……"
            )

        elif status == "__RECOGNIZING__":

            self.stage_status.setText(
                "Arona 正在识别语音……"
            )

    def speech_finished(
        self,
        text,
    ):

        if not text:

            return

        text = text.strip()

        if not text:

            return

        current_text = (
            self.input.toPlainText()
        )

        if current_text:

            self.input.setPlainText(
                current_text
                + " "
                + text
            )

        else:

            self.input.setPlainText(
                text
            )

        self.input.setFocus()

        self.stage_status.setText(
            "语音已经转换成文字啦～"
        )

    def speech_error(
        self,
        error,
    ):

        self.add_message(
            "assistant",
            "🎤 语音输入失败。\n\n"
            + error,
        )

        self.stage_status.setText(
            "语音输入失败，请检查麦克风。"
        )

    def speech_thread_finished(
        self,
    ):

        self.speech_thread = None

        self.speech_worker = None

        self.voice_button.setEnabled(
            True
        )

        self.voice_button.setText(
            "🎤"
        )

    # ========================================================
    # Sidebar / Desktop Transparency
    # ========================================================

    def _capture_desktop_background(
        self,
    ):

        was_visible = self.isVisible()

        if was_visible:

            self.hide()

            QApplication.processEvents()

        screen = QApplication.primaryScreen()

        if screen is not None:

            pixmap = screen.grabWindow(
                0
            )

            if not pixmap.isNull():

                self.desktop_background_pixmap = pixmap

        if was_visible:

            self.show()

            self.raise_()

            self.activateWindow()

        self._update_desktop_background_geometry()

    def _update_desktop_background_geometry(
        self,
    ):

        if self.desktop_background_label is None:

            return

        self.desktop_background_label.setGeometry(
            self.centralWidget().rect()
        )

        if not self.desktop_background_pixmap.isNull():

            self.desktop_background_label.setPixmap(
                self.desktop_background_pixmap
            )

        self.desktop_background_label.lower()

    def resizeEvent(
        self,
        event,
    ):

        super().resizeEvent(
            event
        )

        self._update_desktop_background_geometry()

    def _set_windows_glass_background(
        self,
        enabled,
    ):

        if sys.platform != "win32":

            return

        try:

            hwnd = int(
                self.winId()
            )

            dwmapi = ctypes.windll.dwmapi

            class MARGINS(
                ctypes.Structure
            ):

                _fields_ = [
                    (
                        "cxLeftWidth",
                        ctypes.c_int,
                    ),
                    (
                        "cxRightWidth",
                        ctypes.c_int,
                    ),
                    (
                        "cyTopHeight",
                        ctypes.c_int,
                    ),
                    (
                        "cyBottomHeight",
                        ctypes.c_int,
                    ),
                ]

            if enabled:

                margins = MARGINS(
                    -1,
                    -1,
                    -1,
                    -1,
                )

            else:

                margins = MARGINS(
                    0,
                    0,
                    0,
                    0,
                )

            dwmapi.DwmExtendFrameIntoClientArea(
                wintypes.HWND(hwnd),
                ctypes.byref(margins),
            )

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def _set_windows_desktop_passthrough(
        self,
        enabled,
        alpha=255,
    ):

        if sys.platform != "win32":

            return

        try:

            hwnd = int(
                self.winId()
            )

            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 0x00000002

            get_style = getattr(
                user32,
                "GetWindowLongPtrW",
                None,
            )

            set_style = getattr(
                user32,
                "SetWindowLongPtrW",
                None,
            )

            if get_style is None:

                get_style = user32.GetWindowLongW

            if set_style is None:

                set_style = user32.SetWindowLongW

            hwnd_arg = wintypes.HWND(
                hwnd
            )

            current_style = int(
                get_style(
                    hwnd_arg,
                    GWL_EXSTYLE,
                )
            )

            if enabled:

                current_style |= WS_EX_LAYERED

                set_style(
                    hwnd_arg,
                    GWL_EXSTYLE,
                    current_style,
                )

                alpha = max(
                    1,
                    min(
                        255,
                        int(alpha),
                    ),
                )

                user32.SetLayeredWindowAttributes(
                    hwnd_arg,
                    0,
                    alpha,
                    LWA_ALPHA,
                )

            else:

                if current_style & WS_EX_LAYERED:

                    user32.SetLayeredWindowAttributes(
                        hwnd_arg,
                        0,
                        255,
                        LWA_ALPHA,
                    )

            user32.RedrawWindow(
                hwnd_arg,
                None,
                None,
                0x0001
                | 0x0004
                | 0x0100,
            )

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def _set_transparent_palette(
        self,
        enabled,
    ):

        palette = self.palette()

        if enabled:

            transparent = QBrush(
                Qt.transparent
            )

            palette.setBrush(
                QPalette.Window,
                transparent,
            )

            palette.setBrush(
                QPalette.Base,
                transparent,
            )

            palette.setBrush(
                QPalette.AlternateBase,
                transparent,
            )

            palette.setBrush(
                QPalette.Button,
                transparent,
            )

            palette.setBrush(
                QPalette.Mid,
                transparent,
            )

            palette.setBrush(
                QPalette.Dark,
                transparent,
            )

            palette.setBrush(
                QPalette.Light,
                transparent,
            )

        else:

            palette = QApplication.palette()

        self.setPalette(
            palette
        )

        central = self.centralWidget()

        if central is not None:

            central.setPalette(
                palette
            )

            central.setAttribute(
                Qt.WA_NoSystemBackground,
                bool(enabled),
            )

    def apply_desktop_transparency(
        self,
        enabled,
        alpha=None,
    ):

        self.desktop_transparent = bool(
            enabled
        )

        if alpha is not None:

            self.desktop_background_alpha = max(
                0,
                min(
                    255,
                    int(alpha),
                ),
            )

        # ----------------------------------------------------
        # 这里不再改动窗口的透明属性。
        #
        # 原版在开启 / 关闭透明时反复执行
        #   setAttribute(WA_TranslucentBackground, True)
        # 但值始终是 True，等于什么都没改，所以一直没出问题。
        #
        # 上一轮改成了跟随开关（True / False 来回切），
        # 窗口显示之后再改这个属性，
        # Qt 不会重建原生窗口，
        # 在部分 Windows 驱动上会导致重绘不完整、
        # 局部空白、看起来像控件消失。
        #
        # 现在恢复成「常量为 True、运行时不改动」，
        # 与原版完全一致。
        #
        # WA_NoSystemBackground 之前也只跟随开关，
        # 而 __init__ 里是无条件 True，
        # 于是第一次关闭透明之后窗口状态就和启动时不一致了。
        # 现在同样恢复为常量为 True。
        # ----------------------------------------------------

        self.setAttribute(
            Qt.WA_TranslucentBackground,
            True,
        )

        self.setAttribute(
            Qt.WA_NoSystemBackground,
            True,
        )

        self.setAutoFillBackground(
            False
        )

        central = self.centralWidget()

        if central is not None:

            central.setAttribute(
                Qt.WA_TranslucentBackground,
                True,
            )

            central.setAttribute(
                Qt.WA_NoSystemBackground,
                True,
            )

            central.setAutoFillBackground(
                False
            )

        self._set_transparent_palette(
            True
        )

        if self.desktop_background_label is not None:

            self.desktop_background_label.clear()

            self.desktop_background_label.hide()

            self.desktop_background_label.lower()

        self.apply_style()

        if hasattr(
            self,
            "chat_scroll",
        ):

            for widget in (
                self.chat_scroll,
                self.chat_scroll.viewport(),
                self.chat_container,
            ):

                widget.setAttribute(
                    Qt.WA_OpaquePaintEvent,
                    False,
                )

                widget.setAttribute(
                    Qt.WA_NoSystemBackground,
                    True,
                )

                widget.setAutoFillBackground(
                    False
                )

        if self.desktop_transparent:

            self._set_windows_desktop_passthrough(
                True,
                self.desktop_background_alpha,
            )

        else:

            self._set_windows_desktop_passthrough(
                False,
                255,
            )

        self.update()

    def showEvent(
        self,
        event,
    ):
        """
        窗口第一次显示之后，恢复上次保存的桌面透明背景设置。

        必须等窗口真正显示出来再调用：
        Windows 的透明处理需要有效的窗口句柄
        （_set_windows_desktop_passthrough 里会用到 winId()）。
        """

        super().showEvent(
            event
        )

        if self._desktop_transparency_restored:

            return

        self._desktop_transparency_restored = True

        if self.desktop_transparent:

            # 延后到本次显示流程完全结束再执行。
            #
            # apply_desktop_transparency() 会重建整套样式表
            # 并调用 winId() 做 Windows 层设置，
            # 直接写在 showEvent 里会在首帧绘制途中改样式，
            # 容易留下没重绘干净的区域。
            QTimer.singleShot(
                0,
                self._restore_desktop_transparency,
            )

    def _restore_desktop_transparency(
        self,
    ):
        """
        按已保存的设置恢复桌面透明背景。

        只在启动时调用一次。
        """

        if not self.desktop_transparent:

            return

        self.apply_desktop_transparency(
            True,
            self.desktop_background_alpha,
        )

    def toggle_sidebar(
        self,
    ):

        self.sidebar_visible = not self.sidebar_visible

        self.sidebar.setVisible(
            self.sidebar_visible
        )

        self.sidebar_toggle.setToolTip(
            "隐藏侧边栏"
            if self.sidebar_visible
            else "显示侧边栏"
        )

    # ========================================================
    # Model
    # ========================================================

    def toggle_model_stage(
        self,
        checked,
    ):

        self.model_stage.setVisible(
            checked
        )

        if checked:

            self.model_toggle.setText(
                "◉  3D 模型"
            )

            self.stage_status.setText(
                "Arona 正在等待你的消息～"
            )

        else:

            self.model_toggle.setText(
                "○  3D 模型"
            )

    def fullscreen_model_stage(
        self,
    ):

        if not self.model_stage.isVisible():

            self.model_toggle.setChecked(
                True
            )

            self.model_stage.setVisible(
                True
            )

        dialog = QDialog(
            self
        )

        dialog.setWindowTitle(
            "Arona · 3D 模型"
        )

        dialog.setWindowState(
            Qt.WindowMaximized
        )

        layout = QVBoxLayout(
            dialog
        )

        layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        stage = QFrame()

        stage.setObjectName(
            "fullscreenStage"
        )

        stage_layout = QVBoxLayout(
            stage
        )

        stage_layout.setAlignment(
            Qt.AlignCenter
        )

        title = QLabel(
            "ARONA"
        )

        title.setObjectName(
            "fullscreenTitle"
        )

        title.setAlignment(
            Qt.AlignCenter
        )

        text = QLabel(
            "这里将接入真正的 3D 模型。\n\n"
            "点击右下角关闭全屏。"
        )

        text.setObjectName(
            "fullscreenText"
        )

        text.setAlignment(
            Qt.AlignCenter
        )

        close_button = QPushButton(
            "×"
        )

        close_button.setObjectName(
            "fullscreenClose"
        )

        close_button.clicked.connect(
            dialog.close
        )

        stage_layout.addWidget(
            title
        )

        stage_layout.addWidget(
            text
        )

        stage_layout.addStretch()

        stage_layout.addWidget(
            close_button,
            alignment=Qt.AlignRight,
        )

        layout.addWidget(
            stage
        )

        dialog.exec()

    # ========================================================
    # Chat
    # ========================================================

    def _bubble_rgba(
        self,
        hex_color,
        opacity_percent,
    ):

        color = QColor(
            hex_color
        )

        if not color.isValid():

            color = QColor(
                "#17243A"
            )

        alpha = clamp_int(
            round(
                opacity_percent
                * 255
                / 100
            ),
            1,
            255,
            250,
        )

        return (
            f"rgba("
            f"{color.red()},"
            f"{color.green()},"
            f"{color.blue()},"
            f"{alpha}"
            f")"
        )

    def _create_message_avatar(
        self,
        role,
    ):

        avatar = QLabel()

        if role == "assistant":

            avatar.setObjectName(
                "messageAvatar"
            )

            avatar.setAlignment(
                Qt.AlignCenter
            )

            self._apply_avatar_to_label(
                avatar,
                "",
                "A",
                38,
            )

        else:

            avatar.setObjectName(
                "userAvatar"
            )

            avatar.setAlignment(
                Qt.AlignCenter
            )

            self._apply_avatar_to_label(
                avatar,
                self.user_avatar_path,
                "●",
                38,
            )

        avatar.setFixedSize(
            38,
            38,
        )

        return avatar

    def add_message(
        self,
        role,
        text,
    ):

        row = QHBoxLayout()

        row.setSpacing(
            10
        )

        bubble = QFrame()

        bubble.setObjectName(
            "aiBubble"
            if role == "assistant"
            else "userBubble"
        )

        bubble_layout = QVBoxLayout(
            bubble
        )

        bubble_layout.setContentsMargins(
            14,
            10,
            14,
            10,
        )

        name = QLabel(
            "Arona"
            if role == "assistant"
            else "你"
        )

        name.setObjectName(
            "messageName"
        )

        content = QLabel(
            text
        )

        content.setWordWrap(
            True
        )

        content.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )

        content.setObjectName(
            "messageText"
        )

        bubble_layout.addWidget(
            name
        )

        bubble_layout.addWidget(
            content
        )

        if role == "assistant":

            avatar = (
                self._create_message_avatar(
                    "assistant"
                )
            )

            row.addWidget(
                avatar,
                alignment=Qt.AlignTop,
            )

            row.addWidget(
                bubble
            )

            row.addStretch()

        else:

            row.addStretch()

            row.addWidget(
                bubble
            )

            avatar = (
                self._create_message_avatar(
                    "user"
                )
            )

            row.addWidget(
                avatar,
                alignment=Qt.AlignTop,
            )

        self.chat_layout.insertLayout(
            self.chat_layout.count() - 1,
            row,
        )

        bar = (
            self.chat_scroll
            .verticalScrollBar()
        )

        bar.setValue(
            bar.maximum()
        )

    # ========================================================
    # Chat UI helpers
    # ========================================================

    def clear_chat_messages(
        self,
    ):

        while self.chat_layout.count() > 1:

            item = (
                self.chat_layout.takeAt(
                    0
                )
            )

            child_layout = item.layout()

            if child_layout is not None:

                self.clear_layout(
                    child_layout
                )

            child_widget = item.widget()

            if child_widget is not None:

                child_widget.deleteLater()

    def display_conversation_messages(
        self,
        messages,
    ):

        self.clear_chat_messages()

        for message in messages:

            if not isinstance(
                message,
                dict,
            ):

                continue

            role = message.get(
                "role"
            )

            if role not in (
                "user",
                "assistant",
            ):

                continue

            text = message.get(
                "content",
                "",
            )

            if not isinstance(
                text,
                str,
            ):

                continue

            if not text.strip():

                continue

            self.add_message(
                role,
                text,
            )

    # ========================================================
    # Conversation History
    # ========================================================

    def refresh_history_list(
        self,
        select_current=True,
    ):

        self._refreshing_history = True

        try:

            current_id = (
                self.ai_client.get_current_session_id()
            )

            sessions = (
                self.ai_client.list_conversations()
            )

            self.history_list.clear()

            for session in sessions:

                if not isinstance(
                    session,
                    dict,
                ):

                    continue

                session_id = session.get(
                    "id"
                )

                if not session_id:

                    continue

                title = (
                    session.get(
                        "title"
                    )
                    or "新对话"
                )

                item = QListWidgetItem(
                    title
                )

                item.setData(
                    Qt.UserRole,
                    session_id,
                )

                self.history_list.addItem(
                    item
                )

                if (
                    select_current
                    and session_id == current_id
                ):

                    self.history_list.setCurrentItem(
                        item
                    )

        except Exception as exc:

            self.history_list.clear()

            item = QListWidgetItem(
                "无法加载聊天记录"
            )

            item.setData(
                Qt.UserRole,
                None,
            )

            self.history_list.addItem(
                item
            )

            print(
                "加载聊天记录失败：",
                exc,
            )

        finally:

            self._refreshing_history = False

    def on_history_item_clicked(
        self,
        item,
    ):

        if self._refreshing_history:

            return

        if self._ai_busy:

            return

        session_id = item.data(
            Qt.UserRole
        )

        if not session_id:

            return

        current_id = (
            self.ai_client.get_current_session_id()
        )

        if session_id == current_id:

            self.restore_current_conversation_to_ui()

            return

        try:

            success = (
                self.ai_client.switch_conversation(
                    session_id
                )
            )

            if not success:

                QMessageBox.warning(
                    self,
                    "无法打开对话",
                    "这个聊天记录已经不存在了。",
                )

                self.refresh_history_list()

                return

            self.restore_current_conversation_to_ui()

            session = (
                self.ai_client.get_current_session()
            )

            title = (
                session.get(
                    "title",
                    "新对话",
                )
                if session
                else "新对话"
            )

            self.stage_status.setText(
                f"已切换到「{title}」"
            )

        except Exception as exc:

            QMessageBox.critical(
                self,
                "切换对话失败",
                str(exc),
            )

            self.refresh_history_list()

    def restore_current_conversation_to_ui(
        self,
    ):

        session = (
            self.ai_client.get_current_session()
        )

        if not session:

            self.clear_chat_messages()

            self.stage_status.setText(
                "Arona 正在等待你的消息～"
            )

            return

        messages = session.get(
            "messages",
            [],
        )

        self.display_conversation_messages(
            messages
        )

        title = (
            session.get(
                "title"
            )
            or "新对话"
        )

        if title == "新对话":

            self.stage_status.setText(
                "新的对话开始啦～"
            )

        else:

            self.stage_status.setText(
                f"正在进行「{title}」"
            )

    # ========================================================
    # 发送消息
    # ========================================================

    def send_message(
        self,
    ):

        if self._ai_busy:

            return

        message = (
            self.input
            .toPlainText()
            .strip()
        )

        if not message and not self.attachments:

            return

        self.input.clear()

        current_attachments = [
            dict(item)
            for item in self.attachments
        ]

        if current_attachments:

            attachment_names = "、".join(
                item["name"]
                for item in current_attachments
            )

            if message:

                message_for_display = (
                    f"{message}\n\n"
                    f"📎 附件：{attachment_names}"
                )

            else:

                message_for_display = (
                    f"📎 附件：{attachment_names}"
                )

        else:

            message_for_display = (
                message
            )

        self.add_message(
            "user",
            message_for_display,
        )

        self.stage_status.setText(
            "Arona 正在思考……"
        )

        self._ai_busy = True

        thread = QThread(
            self
        )

        worker = AIWorker(
            self.ai_client,
            message,
            current_attachments,
        )

        worker.moveToThread(
            thread
        )

        thread.started.connect(
            worker.run
        )

        worker.finished.connect(
            self.ai_finished
        )

        worker.error.connect(
            self.ai_error
        )

        worker.finished.connect(
            thread.quit
        )

        worker.error.connect(
            thread.quit
        )

        worker.finished.connect(
            worker.deleteLater
        )

        worker.error.connect(
            worker.deleteLater
        )

        thread.finished.connect(
            thread.deleteLater
        )

        worker_pair = (
            thread,
            worker,
        )

        self.worker_threads.append(
            worker_pair
        )

        thread.finished.connect(
            lambda pair=worker_pair:
            self._remove_worker(
                pair
            )
        )

        thread.start()

        self.clear_attachments()

    def _remove_worker(
        self,
        worker_pair,
    ):

        if worker_pair in self.worker_threads:

            self.worker_threads.remove(
                worker_pair
            )

    # ========================================================
    # AI 完成
    # ========================================================

    def ai_finished(
        self,
        result,
    ):

        self._ai_busy = False

        self.add_message(
            "assistant",
            result,
        )

        self.refresh_history_list(
            select_current=True
        )

        self.stage_status.setText(
            "Arona 正在等待你的消息～"
        )

    def ai_error(
        self,
        error,
    ):

        self._ai_busy = False

        self.add_message(
            "assistant",
            "抱歉，这次请求没有成功。\n\n"
            + error,
        )

        self.refresh_history_list(
            select_current=True
        )

        self.stage_status.setText(
            "AI 请求失败，请检查设置。"
        )

    # ========================================================
    # 新建对话
    # ========================================================

    def new_chat(
        self,
    ):

        if self._ai_busy:

            QMessageBox.information(
                self,
                "请稍等",
                "Arona 还在处理上一条消息，请等请求完成后再新建对话。",
            )

            return

        self.ai_client.create_conversation(
            title="新对话"
        )

        self.clear_chat_messages()

        self.clear_attachments()

        self.input.clear()

        self.refresh_history_list(
            select_current=True
        )

        self.stage_status.setText(
            "新的对话开始啦～"
        )

        self.input.setFocus()

    def clear_layout(
        self,
        layout,
    ):

        while layout.count():

            item = layout.takeAt(
                0
            )

            child = item.widget()

            child_layout = item.layout()

            if child:

                child.deleteLater()

            elif child_layout:

                self.clear_layout(
                    child_layout
                )

    # ========================================================
    # Attachments
    # ========================================================

    def add_image(
        self,
    ):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )

        if path:

            self.add_attachment(
                path,
                "image",
            )

    # ========================================================
    # R12.3-07 M3：图片生成入口（后台线程，G9）
    # ========================================================

    def _image_gen_service(self):
        """惰性构造图片生成服务（门禁不满足时返回 None，零网络）。

        注意：方法名与实例属性 `_image_gen_service` 同名，
        Python 实例属性优先于方法查找。构造成功后将服务存入
        私有槽 `_image_gen_service_obj`，避免属性遮蔽方法。
        """
        if ImageGenerationService is None:
            return None
        if self._image_gen_service_obj is None:
            try:
                self._image_gen_service_obj = ImageGenerationService(
                    settings=self.ai_client.settings,
                    results_dir=RESULTS_DIR,
                )
            except Exception:
                return None
        return self._image_gen_service_obj

    def start_image_generation(self):
        """从聊天输入框读取提示词，提交后台生成任务。"""
        if self._image_gen_busy:
            return

        prompt = self.input.toPlainText().strip()
        if not prompt:
            QMessageBox.information(
                self,
                "请输入描述",
                "请在下方输入你想生成的图片描述。",
            )
            return

        service = self._image_gen_service()
        if service is None:
            self._show_image_gen_error("图片生成模块不可用。")
            return

        ok, err_code = service.is_available()
        if not ok:
            if err_code == "CREDENTIAL_MISSING":
                QMessageBox.information(
                    self,
                    "缺少 API Key",
                    "请先在设置中填入 OpenAI 图片 API Key，并开启图片生成。",
                )
            else:
                msg = _IMAGE_GEN_ERR_MESSAGES.get(
                    err_code, "图片生成当前不可用。"
                )
                self._show_image_gen_error(msg)
            return

        # 占用输入框：清空并把提示作为展示消息。
        self.input.clear()
        self.add_message("user", f"🖼 {prompt}")
        self.stage_status.setText("正在生成图片……")
        self._image_gen_busy = True

        thread = QThread(self)
        worker = ImageGenWorker(service, prompt)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_image_gen_finished)
        worker.error.connect(self._on_image_gen_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        pair = (thread, worker)
        self.image_gen_threads.append(pair)
        thread.finished.connect(
            lambda p=pair: self._remove_image_gen_thread(p)
        )
        thread.start()

    def _on_image_gen_finished(self, path):
        self._image_gen_busy = False
        try:
            self.add_message("assistant", f"✅ 图片已生成：{path.name}")
            self._show_image_in_bubble(str(path))
        except Exception:
            self.add_message("assistant", f"✅ 图片已生成：{path}")
        self.stage_status.setText("图片生成完成～")

    def _on_image_gen_error(self, error_text):
        self._image_gen_busy = False
        self._show_image_gen_error(error_text)

    def _show_image_gen_error(self, text):
        self.add_message("assistant", f"⚠️ {text}")
        self.stage_status.setText("图片生成失败。")

    def _show_image_in_bubble(self, path):
        """把生成图作为附件加进聊天区（复用现有附件渲染）。"""
        try:
            if Path(path).exists():
                self.add_attachment(path, "image")
        except Exception:
            pass

    def _remove_image_gen_thread(self, pair):
        if pair in self.image_gen_threads:
            self.image_gen_threads.remove(pair)

    def _stop_image_gen_threads(self):
        """窗口关闭时收尾图片生成线程（G9：不阻塞 UI 退出）。

        有界等待 1.5s；若线程仍未停止（网络阻塞中），
        从追踪列表移除以保留线程引用，避免 deleteLater 销毁
        仍被 worker 引用中的 QThread（防止 worker 发信号到已析构线程）。
        与 _stop_background_threads 的 orphan 策略一致。
        """
        for pair in list(getattr(self, "image_gen_threads", [])):
            thread, worker = pair[0], pair[1]
            try:
                thread.quit()
            except Exception:
                pass
            try:
                if not thread.wait(1500):
                    # 线程仍在运行（网络请求阻塞中）：保留引用，
                    # 不 deleteLater，避免 use-after-free。
                    self.image_gen_threads.remove(pair)
                    logger.warning(
                        "图片生成线程未在 1.5s 内停止，已保留引用避免销毁。"
                    )
            except Exception:
                pass

    def add_video(
        self,
    ):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择视频",
            "",
            "视频 (*.mp4 *.mov *.avi *.mkv *.webm)",
        )

        if path:

            self.add_attachment(
                path,
                "video",
            )

    def add_file(
        self,
    ):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择文件",
            "",
            "所有文件 (*.*)",
        )

        if path:

            self.add_attachment(
                path,
                "file",
            )

    def _copy_attachment_to_workspace(
        self,
        path,
    ):

        ensure_local_directories()

        source = Path(
            path
        )

        if not source.exists():

            return path

        target = (
            UPLOADS_DIR
            / source.name
        )

        # 如果同名，自动创建不会覆盖原文件的副本。
        if target.exists():

            stem = source.stem
            suffix = source.suffix

            counter = 1

            while True:

                candidate = (
                    UPLOADS_DIR
                    / f"{stem}_{counter}{suffix}"
                )

                if not candidate.exists():

                    target = candidate

                    break

                counter += 1

        try:

            shutil.copy2(
                source,
                target,
            )

            return str(
                target
            )

        except Exception as exc:

            print(
                "复制附件到 workspace 失败：",
                exc,
            )

            return path

    def add_attachment(
        self,
        path,
        kind,
    ):

        workspace_path = (
            self._copy_attachment_to_workspace(
                path
            )
        )

        self.attachments.append(
            {
                "path": workspace_path,
                "name": Path(
                    path
                ).name,
                "kind": kind,
                "source_path": path,
            }
        )

        self.refresh_attachment_row()

    def refresh_attachment_row(
        self,
    ):

        while self.attachment_row.count():

            item = (
                self.attachment_row.takeAt(
                    0
                )
            )

            widget = item.widget()

            if widget:

                widget.deleteLater()

        for index, attachment in enumerate(
            self.attachments
        ):

            chip = QFrame()

            chip.setObjectName(
                "attachmentChip"
            )

            chip_layout = QHBoxLayout(
                chip
            )

            chip_layout.setContentsMargins(
                10,
                5,
                5,
                5,
            )

            chip_layout.setSpacing(
                6
            )

            if attachment[
                "kind"
            ] == "image":

                icon = "🖼"

            elif attachment[
                "kind"
            ] == "video":

                icon = "🎬"

            else:

                icon = "📄"

            label = QLabel(
                f"{icon} {attachment['name']}"
            )

            remove = QToolButton()

            remove.setText(
                "×"
            )

            remove.clicked.connect(
                lambda checked=False,
                i=index:
                self.remove_attachment(
                    i
                )
            )

            chip_layout.addWidget(
                label
            )

            chip_layout.addWidget(
                remove
            )

            self.attachment_row.addWidget(
                chip
            )

        self.attachment_row.addStretch()

    def remove_attachment(
        self,
        index,
    ):

        if 0 <= index < len(
            self.attachments
        ):

            self.attachments.pop(
                index
            )

            self.refresh_attachment_row()

    def clear_attachments(
        self,
    ):

        self.attachments.clear()

        self.refresh_attachment_row()

    # ========================================================
    # Settings
    # ========================================================

    def open_settings(
        self,
    ):

        _diag = {}
        _t_post_exec_start = None
        _t_save_start = None
        _t_save_end = None
        _last_stage = None
        _delete_later_attempted = False
        _delete_later_ok = False
        _exception_cleanup_done = False

        def _fmt(v):
            """耗时/状态值格式化：None→N/A，bool→True/False，str→原样，数值→ms。"""
            if v is None:
                return "N/A"
            if isinstance(v, bool):
                return str(v)
            if isinstance(v, str):
                return v
            return f"{v:.1f}ms"

        # ── 诊断辅助导入（置于最前，失败时记录并立即传播）────────
        _import_ok = True
        try:
            import time as _t
            import shiboken6 as _shiboken
            import traceback as _tb
        except Exception as _imp_exc:
            _import_ok = False
            _diag["_import_error"] = f"{type(_imp_exc).__name__}: {_imp_exc}"
            print(f"[SAVE_DIAG] IMPORT FAILURE: {_diag['_import_error']}", flush=True)
            raise

        try:
            # ── 阶段 A：构造对话框 ────────────────────────────
            _t_a0 = _t.monotonic()
            _last_stage = "dialog_create"
            try:
                dialog = SettingsDialog(
                    self.ai_client,
                    self,
                )
                # 将对话框的感知开关信号连接到主窗口的处理逻辑。
                dialog.awareness_toggled.connect(
                    self._on_awareness_toggled
                )
                # 同步开关初始状态：以当前设置值为准，反映主窗口实际状态。
                if hasattr(dialog, "awareness_enabled_checkbox"):
                    aw_settings = (
                        self.ai_client.settings.settings
                        .get("desktop_awareness", {})
                    )
                    dialog.awareness_enabled_checkbox.setChecked(
                        bool(aw_settings.get("enabled", False))
                    )
            finally:
                _t_a1 = _t.monotonic()
                _diag["dialog_create_ms"] = (_t_a1 - _t_a0) * 1000

            # ── 阶段 B：等待用户操作 ──────────────────────────
            # exec() 自身异常时，_t_post_exec_start 不被设置，
            # post_exec_total_ms 不计算。
            _t_b0 = _t.monotonic()
            _last_stage = "dialog_exec"
            try:
                result = dialog.exec()
            except Exception:
                _diag["dialog_exec_ms"] = (_t.monotonic() - _t_b0) * 1000
                raise
            _diag["dialog_exec_ms"] = (_t.monotonic() - _t_b0) * 1000
            # exec 正常返回后，才开始统计后续处理耗时
            _t_post_exec_start = _t.monotonic()

            # ── 安全核查：exec() 返回后 dialog 是否仍存活 ──
            # WA_DeleteOnClose 下 accept()/close() 触发 deleteLater() 排队，
            # exec() 的嵌套 event loop 退出时尚未实际销毁，isValid 预期为 True。
            # 仅记录观测值，不作根因推断。
            _diag["dialog_valid_after_exec"] = _shiboken.isValid(dialog)

            if result == QDialog.Accepted:

                if not _shiboken.isValid(dialog):
                    # ── 异常防护分支：exec 返回 Accepted 但 dialog 已失效 ──
                    # 不对已失效的 Qt 对象调用任何实例方法。
                    # 此为防御性补充，不声称与原逻辑完全等价。
                    _diag["save_result"] = "Accepted_invalid_dialog"
                    _diag["_abnormal_state"] = (
                        "dialog exec returned Accepted but is no longer valid; "
                        "skipping all business stages"
                    )
                else:
                    # ── 进入有效 Accepted 分支：开始保存处理计时 ──
                    _t_save_start = _t.monotonic()

                    # ── 阶段 C：读取对话框控件值 ──────────────
                    _t_c0 = _t.monotonic()
                    _last_stage = "read_dialog_values"
                    try:
                        enabled = (
                            dialog.desktop_transparent_checkbox.isChecked()
                        )
                        alpha = int(
                            dialog.opacity_slider.value()
                            * 255
                            / 100
                        )
                    finally:
                        _t_c1 = _t.monotonic()
                        _diag["read_dialog_values_ms"] = (_t_c1 - _t_c0) * 1000

                    # ── 阶段 D：apply_desktop_transparency ────
                    # 内部包含第一次 apply_style() + Win32 API
                    _t_d0 = _t.monotonic()
                    _last_stage = "apply_desktop_transparency"
                    try:
                        self.apply_desktop_transparency(
                            enabled,
                            alpha,
                        )
                    finally:
                        _t_d1 = _t.monotonic()
                        _diag["apply_desktop_transparency_ms"] = (_t_d1 - _t_d0) * 1000

                    # ── 阶段 E：加载外观配置 ──────────────────
                    _t_e0 = _t.monotonic()
                    _last_stage = "load_appearance"
                    try:
                        self.appearance_settings = (
                            load_appearance_settings()
                        )
                        self.appearance_settings = (
                            self._normalize_appearance_settings()
                        )
                    finally:
                        _t_e1 = _t.monotonic()
                        _diag["load_appearance_ms"] = (_t_e1 - _t_e0) * 1000

                    # ── 阶段 F：获取头像路径 ──────────────────
                    _t_f0 = _t.monotonic()
                    _last_stage = "get_avatar_path"
                    try:
                        self.user_avatar_path = (
                            self._get_existing_avatar_path()
                        )
                    finally:
                        _t_f1 = _t.monotonic()
                        _diag["get_avatar_path_ms"] = (_t_f1 - _t_f0) * 1000

                    # ── 阶段 G：第二次 apply_style() ──────────
                    # 应用刚从磁盘加载的外观配置（非冗余，见诊断方案分析）
                    _t_g0 = _t.monotonic()
                    _last_stage = "apply_style_2nd"
                    try:
                        self.apply_style()
                    finally:
                        _t_g1 = _t.monotonic()
                        _diag["apply_style_2nd_ms"] = (_t_g1 - _t_g0) * 1000

                    # ── 阶段 H：刷新头像和聊天消息 ────────────
                    _t_h0 = _t.monotonic()
                    _last_stage = "refresh_avatar_widgets"
                    try:
                        self.refresh_avatar_widgets()
                    finally:
                        _t_h1 = _t.monotonic()
                        _diag["refresh_avatar_widgets_ms"] = (_t_h1 - _t_h0) * 1000

                    # ── 阶段 I：刷新模型卡片 ──────────────────
                    _t_i0 = _t.monotonic()
                    _last_stage = "refresh_model_card"
                    try:
                        self.refresh_model_card()
                    finally:
                        _t_i1 = _t.monotonic()
                        _diag["refresh_model_card_ms"] = (_t_i1 - _t_i0) * 1000

                    # ── 阶段 K：设置状态文字 ──────────────────
                    _t_k0 = _t.monotonic()
                    _last_stage = "stage_set_text"
                    try:
                        self.stage_status.setText(
                            "设置已保存，Arona 已准备好啦～"
                        )
                    finally:
                        _t_k1 = _t.monotonic()
                        _diag["stage_set_text_ms"] = (_t_k1 - _t_k0) * 1000

                    # save_processing_ms 终点：setText() 返回后立即记录，
                    # 不包含 deleteLater()、日志格式化或打印时间
                    _t_save_end = _t.monotonic()
                    _diag["save_result"] = "Accepted"

            elif result == QDialog.Rejected:
                _diag["save_result"] = "Rejected (user cancelled)"
            else:
                _diag["save_result"] = f"Other result={result}"

            # ----------------------------------------------------
            # 用完就销毁设置窗口。
            #
            # 它是以主窗口为 parent 创建的，又没有设置
            # WA_DeleteOnClose，所以 Qt 不会自动回收：
            # 每打开一次设置，就永久多留下一个隐藏的对话框
            # 以及它内部的 91 个控件（实测数据）。
            #
            # 连开 5 次的实测结果：
            #   主窗口子控件   71 -> 526
            #   存活 QDialog    0 -> 5
            #   apply_style()  50.7ms -> 374.3ms
            #
            # 而 apply_style() 在每次保存设置、切换透明背景时
            # 都会执行，越用越慢，
            # 表现出来就是界面重绘不干净、像控件显示异常。
            #
            # 必须放在读完 dialog 上的值之后再调用，
            # 否则会访问已经析构的对象。
            # ----------------------------------------------------

            # ── 阶段 J：原有清理调用（保留不变，位于 if 块之外）────────
            # 统一检查有效性：不论 Accepted、Rejected 或其他结果，
            # 对已失效对象均不得调用 dialog.deleteLater()。
            #
            # 异常策略：捕获 deleteLater() 可能抛出的异常并记录，
            # 不重新抛出，不阻塞诊断流程。
            # _delete_later_attempted 标志表示"清理阶段已尝试进入"，
            # 不等于"对象已成功销毁"。
            _t_j0 = _t.monotonic()
            _last_stage = "delete_later"
            _delete_later_attempted = False
            _delete_later_ok = False
            try:
                if 'dialog' in locals():
                    try:
                        if _shiboken.isValid(dialog):
                            dialog.deleteLater()
                            _delete_later_ok = True
                    except Exception as _del_exc:
                        _diag["delete_later_error"] = (
                            f"{type(_del_exc).__name__}: {_del_exc}"
                        )
                    _delete_later_attempted = True
            finally:
                _t_j1 = _t.monotonic()
                _diag["delete_later_ms"] = (_t_j1 - _t_j0) * 1000
                _diag["delete_later_attempted"] = _delete_later_attempted
                _diag["delete_later_ok"] = _delete_later_ok

            # ── 计算总耗时指标（必须在打印前完成）─────────────
            # post_exec_total_ms：exec 正常返回后至清理结束的全部耗时
            if _t_post_exec_start is not None:
                _t_post_end = _t.monotonic()
                _diag["post_exec_total_ms"] = (
                    _t_post_end - _t_post_exec_start
                ) * 1000

            # save_processing_ms：仅 Accepted + dialog 有效 分支的业务处理耗时
            # 起点 = _t_save_start，终点 = stage_status.setText() 返回后
            # 不包含 deleteLater()、日志格式化、打印
            if _t_save_start is not None:
                if _t_save_end is not None:
                    _diag["save_processing_ms"] = (
                        _t_save_end - _t_save_start
                    ) * 1000
                else:
                    # setText 未执行（异常发生在保存阶段）
                    # 此分支在正常路径不可达，仅在 except 中使用
                    _diag["save_processing_ms"] = "pending"
            else:
                _diag["save_processing_ms"] = "N/A"

            # ── 输出诊断摘要（包含全部 13 个字段）─────────────
            # 格式化与打印本身的时间不计入任何计时指标。
            _line = (
                "[SAVE_DIAG] "
                f"exec={_fmt(_diag.get('dialog_exec_ms'))} "
                f"(user_wait,not_freeze) | "
                f"create={_fmt(_diag.get('dialog_create_ms'))} "
                f"read={_fmt(_diag.get('read_dialog_values_ms'))} "
                f"transparency={_fmt(_diag.get('apply_desktop_transparency_ms'))} "
                f"load={_fmt(_diag.get('load_appearance_ms'))} "
                f"avatar={_fmt(_diag.get('get_avatar_path_ms'))} "
                f"style2={_fmt(_diag.get('apply_style_2nd_ms'))} "
                f"refresh={_fmt(_diag.get('refresh_avatar_widgets_ms'))} "
                f"model_card={_fmt(_diag.get('refresh_model_card_ms'))} "
                f"set_text={_fmt(_diag.get('stage_set_text_ms'))} "
                f"delete_later={_fmt(_diag.get('delete_later_ms'))} "
                f"dl_ok={_fmt(_diag.get('delete_later_ok'))} "
                f"| post_exec_total={_fmt(_diag.get('post_exec_total_ms'))} "
                f"save_proc={_fmt(_diag.get('save_processing_ms'))} "
                f"result={_diag.get('save_result', 'N/A')} "
                f"dialog_valid={_fmt(_diag.get('dialog_valid_after_exec'))} "
                f"last_stage={_last_stage}"
            )
            print(_line, flush=True)

        except Exception as _exc:
            # 统一入口捕获时间点，确保两个指标使用相同基准
            try:
                _t_exc_end = _t.monotonic()
            except Exception:
                _t_exc_end = 0

            _exc_info = f"{type(_exc).__name__}: {_exc}"
            _diag["_exception"] = _exc_info
            _diag["_last_stage"] = _last_stage

            # post_exec_total_ms：仅当 exec 曾正常返回
            if _t_post_exec_start is not None:
                _diag["post_exec_total_ms"] = (
                    _t_exc_end - _t_post_exec_start
                ) * 1000

            # save_processing_ms：
            # - 若 _t_save_end 已设置（setText 成功），使用 _t_save_end - _t_save_start
            # - 若 _t_save_end 未设置但 _t_save_start 已设置（保存阶段异常），
            #   使用 _t_exc_end - _t_save_start
            # - 若 _t_save_start 未设置（未进入保存分支），输出 N/A
            if _t_save_start is not None and _t_save_end is not None:
                _diag["save_processing_ms"] = (
                    _t_save_end - _t_save_start
                ) * 1000
            elif _t_save_start is not None:
                _diag["save_processing_ms"] = (
                    _t_exc_end - _t_save_start
                ) * 1000
            else:
                _diag["save_processing_ms"] = "N/A"

            # ── 异常路径清理策略 ──────────────────────────────
            # 若 exec() 抛异常且对话框已成功构造，对话框可能处于可见状态。
            # 原代码对此无特殊处理（直接让异常传播）。
            # 此处增加防御性 close() 以触发 WA_DeleteOnClose，
            # 避免对话框在异常后保持可见。
            # 仅当阶段 J 尚未执行时（_delete_later_attempted=False）才进行此清理。
            if (
                not _delete_later_attempted
                and 'dialog' in locals()
                and not _exception_cleanup_done
            ):
                try:
                    _t_exc_cleanup0 = _t.monotonic()
                    if _shiboken.isValid(dialog):
                        try:
                            dialog.close()
                            _diag["_exception_cleanup_ok"] = True
                        except Exception as _close_exc:
                            _diag["_exception_cleanup_error"] = (
                                f"{type(_close_exc).__name__}: {_close_exc}"
                            )
                            _diag["_exception_cleanup_ok"] = False
                    else:
                        _diag["_exception_cleanup_ok"] = None
                        _diag["_exception_cleanup_skip"] = "dialog invalid"
                    _t_exc_cleanup1 = _t.monotonic()
                    _diag["_exception_cleanup_ms"] = (
                        _t_exc_cleanup1 - _t_exc_cleanup0
                    ) * 1000
                    _exception_cleanup_done = True
                except Exception as _cleanup_exc:
                    _diag["_exception_cleanup_failure"] = (
                        f"{type(_cleanup_exc).__name__}: {_cleanup_exc}"
                    )

            print(f"[SAVE_DIAG] EXCEPTION: {_exc_info}", flush=True)
            print("[SAVE_DIAG] traceback:", flush=True)
            _tb.print_exc()

            # 异常路径也输出格式化摘要，未执行阶段显示 N/A
            _eline = (
                "[SAVE_DIAG] "
                f"exec={_fmt(_diag.get('dialog_exec_ms'))} "
                f"(user_wait,not_freeze) | "
                f"create={_fmt(_diag.get('dialog_create_ms'))} "
                f"read={_fmt(_diag.get('read_dialog_values_ms'))} "
                f"transparency={_fmt(_diag.get('apply_desktop_transparency_ms'))} "
                f"load={_fmt(_diag.get('load_appearance_ms'))} "
                f"avatar={_fmt(_diag.get('get_avatar_path_ms'))} "
                f"style2={_fmt(_diag.get('apply_style_2nd_ms'))} "
                f"refresh={_fmt(_diag.get('refresh_avatar_widgets_ms'))} "
                f"model_card={_fmt(_diag.get('refresh_model_card_ms'))} "
                f"set_text={_fmt(_diag.get('stage_set_text_ms'))} "
                f"delete_later={_fmt(_diag.get('delete_later_ms'))} "
                f"dl_ok={_fmt(_diag.get('delete_later_ok'))} "
                f"| post_exec_total={_fmt(_diag.get('post_exec_total_ms'))} "
                f"save_proc={_fmt(_diag.get('save_processing_ms'))} "
                f"result={_diag.get('save_result', 'N/A')} "
                f"dialog_valid={_fmt(_diag.get('dialog_valid_after_exec'))} "
                f"last_stage={_last_stage}"
            )
            print(_eline, flush=True)
            print(f"[SAVE_DIAG] partial_diag={_diag}", flush=True)
            # 重新抛出原始异常，不吞掉
            raise

    # ========================================================
    # 桌面截图（V0.2 第一阶段）
    # ========================================================

    def capture_desktop_for_ai(
        self,
    ):
        """
        截取当前桌面，经用户确认后交给视觉模型分析。

        安全约定：
        * 截图只存在于内存里，默认不写盘。
        * 未经用户确认，不会把任何图片发送到云端。
        * 模型不支持图像输入时明确提示，并且不发送。
        """

        if DesktopVision is None:

            QMessageBox.warning(
                self,
                "功能不可用",
                "桌面感知模块加载失败，该功能暂时无法使用。",
            )

            return

        # ------------------------------------------------
        # 截图（内存中，不落盘）
        # ------------------------------------------------

        try:

            vision = DesktopVision(
                self.ai_client
            )

            # 截图前先把自己藏起来。
            #
            # 不隐藏的话，AronaClient 自己的窗口会盖住桌面，
            # 截出来几乎全是它自己
            # （实测：最大化时 84% 的画面是自身界面）。
            #
            # 隐藏、等桌面重绘、截图、恢复窗口，
            # 全部由 temporarily_hidden 负责，
            # 取消和异常也一定会恢复窗口。
            shot = vision.capture(
                hide_widgets=[self]
            )

        except Exception as exc:

            QMessageBox.critical(
                self,
                "截图失败",
                f"{type(exc).__name__}: {exc}",
            )

            return

        if not shot.success:

            QMessageBox.warning(
                self,
                "截图失败",
                shot.error or "未知错误。",
            )

            return

        support = vision.check_vision_support()

        try:

            provider = (
                self.ai_client.get_provider()
                or ""
            )

        except Exception:

            provider = ""

        # ------------------------------------------------
        # 用户确认
        # ------------------------------------------------

        dialog = DesktopCaptureDialog(
            self,
            shot,
            support,
            provider,
        )

        accepted = dialog.exec()

        question = dialog.get_question()

        dialog.deleteLater()

        if accepted != QDialog.Accepted or not support.supported:

            shot.release()

            self.stage_status.setText(
                "已取消发送截图。"
            )

            return

        # ------------------------------------------------
        # 后台分析
        # ------------------------------------------------

        if self._desktop_vision_busy:
            shot.release()
            self.stage_status.setText(
                "上一张截图分析尚未完成，请稍后再试。"
            )
            return

        self._desktop_vision_busy = True
        self._desktop_shot = shot

        self.add_message(
            "user",
            "［桌面截图］"
            + (
                question
                or "请描述我当前的电脑桌面。"
            ),
        )

        self.stage_status.setText(
            "Arona 正在查看你的桌面……"
        )

        thread = QThread(
            self
        )

        worker = DesktopVisionWorker(
            vision,
            shot,
            question,
            # 执行到这里说明用户已经在 DesktopCaptureDialog 里点了确认
            # （上方 7230 行的 early-return 已经挡住了取消分支）。
            user_confirmed=True,
        )

        worker.moveToThread(
            thread
        )

        thread.started.connect(
            worker.run
        )

        worker.finished.connect(
            self._on_desktop_vision_finished
        )

        worker.finished.connect(
            thread.quit
        )

        worker.finished.connect(
            worker.deleteLater
        )

        thread.finished.connect(
            thread.deleteLater
        )

        worker_pair = (
            thread,
            worker,
        )

        self.worker_threads.append(
            worker_pair
        )

        thread.finished.connect(
            lambda pair=worker_pair:
            self._remove_worker(
                pair
            )
        )

        thread.start()

    def _on_desktop_vision_finished(
        self,
        result,
    ):
        """
        处理桌面截图分析结果。

        失败时只显示真实错误，绝不编造识别内容。
        """

        shot = getattr(
            self,
            "_desktop_shot",
            None,
        )

        if shot is not None:

            shot.release()

        self._desktop_shot = None
        self._desktop_vision_busy = False

        if not isinstance(result, dict):

            result = {
                "success": False,
                "text": "",
                "error": "分析返回了无法识别的结果。",
                "hint": None,
                "sent": False,
            }

        if result.get("success"):

            self.add_message(
                "assistant",
                result.get("text") or "",
            )

            self.stage_status.setText(
                "桌面截图分析完成。"
            )

            return

        lines = [
            "这次没能看到你的桌面。",
            "",
            f"原因：{result.get('error')}",
        ]

        if not result.get("sent"):

            lines.append("")
            lines.append("（没有把任何图片发送到云端）")

        if result.get("hint"):

            lines.append("")
            lines.append(
                result["hint"]
            )

        self.add_message(
            "assistant",
            "\n".join(lines),
        )

        self.stage_status.setText(
            "桌面截图分析未完成。"
        )

    # ── V0.3 桌面自主感知生命周期 ─────────────────────────

    def _init_desktop_awareness(self) -> None:
        """
        初始化桌面自主感知引擎。
        仅在模块可用、DV 授权已开启、且用户在设置中启用时启动。
        """
        if DesktopAwareness is None:
            return

        # DV 授权是持续感知的前提：未授权时静默跳过，不启动引擎。
        # 此处直接读 settings（与 _desktop_vision_snapshot 一致的 fail-closed 逻辑）。
        if not self.ai_client.settings.get_desktop_vision_enabled():
            logger.debug("桌面自主感知：DV 未授权，跳过初始化。")
            return

        try:
            aw_settings = (
                self.ai_client.settings.settings
                .get("desktop_awareness", {})
            )
            enabled = bool(aw_settings.get("enabled", False))
            interval = int(
                aw_settings.get("interval_seconds", 30)
            )

            awareness = DesktopAwareness(
                ai_client=self.ai_client,
                interval_seconds=interval,
                on_alert=self._on_awareness_alert,
                on_state_change=self._on_awareness_state_change,
            )

            self.desktop_awareness = awareness
            self._awareness_init_done = True

            if enabled:
                awareness.start()
                self._awareness_active = True
                self._update_awareness_status_ui()

        except Exception as exc:
            logger.warning(
                "桌面自主感知初始化失败：%s", exc
            )

    def _on_awareness_alert(
        self,
        event: any,
    ) -> None:
        """
        感知引擎回调：当检测到重要变化时调用。
        在主线程安全执行。

        行为：
          1. 添加系统通知消息（区分于 AI 回复）
          2. 触发 AI 主动回复（非阻塞，通过 AwarenessReplyWorker）
          3. 更新状态栏
        """
        if not hasattr(self, "stage_status"):
            return

        description = event.summary()
        if not description:
            return

        logger.info(
            "桌面感知：触发提醒，描述=%s", description[:80]
        )

        # 1. 添加系统通知消息（带标记，区别于 AI 回复）
        self.add_message(
            "assistant",
            f"[桌面感知] {description}",
        )

        # 2. 触发 AI 主动回复（不阻塞主线程）
        if not self._awareness_reply_busy:
            self._submit_awareness_reply(description)
        else:
            logger.info(
                "桌面感知：AI 正在处理其他请求，跳过本次主动回复"
            )

        # 3. 更新状态栏
        state = self.desktop_awareness.state.value if self.desktop_awareness else "unknown"
        self.stage_status.setText(
            f"桌面感知活跃（{state}）"
        )

    def _submit_awareness_reply(
        self,
        description: str,
    ) -> None:
        """
        提交桌面感知事件给 AI 进行主动回复。
        非阻塞：在后台线程执行，结果通过信号回调。
        """
        self._awareness_reply_busy = True
        self.stage_status.setText(
            "桌面感知：Arona 正在思考……"
        )
        logger.info("桌面感知：提交 AI 回复请求")

        thread = QThread(self)
        worker = AwarenessReplyWorker(
            self.ai_client,
            description,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        worker.finished.connect(self._on_awareness_reply_finished)
        worker.error.connect(self._on_awareness_reply_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.awareness_reply_threads.append(
            (thread, worker)
        )
        thread.finished.connect(
            lambda pair=(thread, worker):
            self._remove_awareness_reply_thread(pair)
        )

        thread.start()

    def _on_awareness_reply_finished(
        self,
        result: str,
    ) -> None:
        """AI 主动回复完成，显示在聊天中。"""
        self._awareness_reply_busy = False
        logger.info("桌面感知：AI 回复完成")
        self.add_message(
            "assistant",
            result,
        )
        self._update_awareness_status_ui()

    def _on_awareness_reply_error(
        self,
        error: str,
    ) -> None:
        """AI 主动回复失败。"""
        self._awareness_reply_busy = False
        logger.warning("桌面感知：AI 回复失败：%s", error)
        self.add_message(
            "assistant",
            f"[桌面感知] AI 回复失败：{error}",
        )
        self._update_awareness_status_ui()

    def _remove_awareness_reply_thread(
        self,
        thread_pair,
    ) -> None:
        """清理已结束的感知回复线程。"""
        if thread_pair in self.awareness_reply_threads:
            self.awareness_reply_threads.remove(thread_pair)

    def _on_awareness_state_change(
        self,
        new_state: any,
    ) -> None:
        """状态变化回调，用于更新 UI 状态栏。"""
        if not hasattr(self, "stage_status"):
            return
        self._update_awareness_status_ui()

    def _update_awareness_status_ui(self) -> None:
        """根据当前状态更新状态栏文本。"""
        if not hasattr(self, "stage_status"):
            return
        aw = self.desktop_awareness
        if aw is None:
            return
        state = aw.state.value
        labels = {
            "running": "桌面感知运行中",
            "paused": "桌面感知已暂停",
            "stopped": "桌面感知已停止",
        }
        self.stage_status.setText(
            labels.get(state, f"桌面感知: {state}")
        )

    def _stop_desktop_awareness(self) -> None:
        """停止桌面自主感知，在窗口关闭时调用。"""
        if self.desktop_awareness is not None:
            try:
                self.desktop_awareness.stop()
            except Exception:
                pass
            # 确认线程是否真的退出（而非仅仅请求退出）
            _thread = self.desktop_awareness._thread
            _thread_alive = _thread.isRunning() if _thread is not None else False
            if _thread_alive:
                logger.warning(
                    "桌面感知：窗口关闭时感知线程仍在运行，可能延迟退出。"
                )
            self.desktop_awareness = None
            self._awareness_active = False
            self._awareness_init_done = False

    def _on_awareness_toggled(self, checked: bool) -> None:
        """
        桌面自主感知开关切换回调。
        开启时启动或恢复引擎，关闭时暂停引擎。
        幂等：重复操作不会重复创建线程。
        """
        if checked:
            # 开启：确保引擎在运行
            if self.desktop_awareness is None:
                # 从未初始化过，执行完整初始化
                self._init_desktop_awareness()
            elif self.desktop_awareness.is_stopped:
                # 已停止（如 DV 曾关闭后重新开启），重新初始化
                self._init_desktop_awareness()
            elif self.desktop_awareness.is_paused:
                # 已暂停，恢复
                try:
                    self.desktop_awareness.resume()
                    self._awareness_active = True
                    self._update_awareness_status_ui()
                    logger.info("桌面感知：用户手动恢复")
                except Exception as exc:
                    logger.warning("桌面感知：恢复失败：%s", exc)
            # else: 已在运行，无需操作
        else:
            # 关闭：暂停引擎（不销毁，保留状态以便恢复）
            if self.desktop_awareness is not None and self.desktop_awareness.is_running:
                try:
                    self.desktop_awareness.pause()
                    self._awareness_active = False
                    self._update_awareness_status_ui()
                    logger.info("桌面感知：用户手动暂停")
                except Exception as exc:
                    logger.warning("桌面感知：暂停失败：%s", exc)

    def refresh_avatar_widgets(
        self,
    ):

        if hasattr(
            self,
            "avatar",
        ):

            self._apply_avatar_to_label(
                self.avatar,
                "",
                "A",
                38,
            )

        self.clear_chat_messages()

        session = (
            self.ai_client.get_current_session()
        )

        if session:

            self.display_conversation_messages(
                session.get(
                    "messages",
                    [],
                )
            )

    def refresh_model_card(
        self,
    ):

        provider = (
            self.ai_client.get_provider()
        )

        model = (
            self.ai_client.get_model()
        )

        configured = (
            self.ai_client.is_configured()
        )

        provider_name = (
            "DeepSeek"
            if provider == "deepseek"
            else "OpenRouter"
        )

        self.model_info_label.setText(
            f"{provider_name} · {model}"
        )

        if configured:

            self.model_status_label.setText(
                "● 已连接"
            )

            self.model_status_label.setObjectName(
                "modelStatusConnected"
            )

        else:

            self.model_status_label.setText(
                "● 未配置"
            )

            self.model_status_label.setObjectName(
                "modelStatus"
            )

        self.model_status_label.style().unpolish(
            self.model_status_label
        )

        self.model_status_label.style().polish(
            self.model_status_label
        )

    # ========================================================
    # Keyboard
    # ========================================================

    def eventFilter(
        self,
        obj,
        event,
    ):

        if (
            obj is self.input
            and event.type()
            == QEvent.KeyPress
        ):

            if event.key() in (
                Qt.Key_Return,
                Qt.Key_Enter,
            ):

                if (
                    event.modifiers()
                    & Qt.ShiftModifier
                ):

                    return False

                self.send_message()

                return True

        # ----------------------------------------------------
        # 自定义聊天背景图片
        #
        # chat_scroll 的 viewport 会重绘 / 改变尺寸时，
        # 按当前实际大小重新裁切绘制背景图。
        # ----------------------------------------------------

        chat_scroll = getattr(
            self,
            "chat_scroll",
            None,
        )

        if (
            chat_scroll is not None
            and obj is chat_scroll.viewport()
        ):

            event_type = event.type()

            if event_type == QEvent.Paint:

                self._paint_chat_background()

            elif event_type == QEvent.Resize:

                chat_scroll.viewport().update()

        return super().eventFilter(
            obj,
            event,
        )

    # ========================================================
    # Style
    # ========================================================

    def _chat_background_qss(
        self,
        default_background,
    ):

        mode = self.appearance_settings.get(
            "chat_background_mode",
            "default",
        )

        opacity = clamp_int(
            self.appearance_settings.get(
                "chat_background_opacity",
                100,
            ),
            0,
            100,
            100,
        )

        if mode == "color":

            color = QColor(
                self.appearance_settings.get(
                    "chat_background_color",
                    "#0B1220",
                )
            )

            if not color.isValid():

                color = QColor(
                    "#0B1220"
                )

            alpha = round(
                opacity
                * 255
                / 100
            )

            return (
                f"rgba("
                f"{color.red()},"
                f"{color.green()},"
                f"{color.blue()},"
                f"{alpha}"
                f")"
            )

        if mode == "image":

            image_path = (
                self.appearance_settings.get(
                    "chat_background_image",
                    "",
                )
            )

            if image_path and Path(
                image_path
            ).exists():

                normalized_path = (
                    Path(
                        image_path
                    )
                    .resolve()
                    .as_posix()
                    .replace(
                        "'",
                        "\\'",
                    )
                )

                # 使用一个透明颜色作为底层，
                # 图片透明度由 overlay 实现。
                return (
                    f"transparent;"
                    f"background-image: url('{normalized_path}');"
                    f"background-position: center;"
                    f"background-repeat: no-repeat;"
                    f"background-attachment: fixed;"
                    f"background-size: cover;"
                )

        return default_background

    def apply_style(
        self,
    ):

        if self.desktop_transparent:

            background = "transparent"

            panel_alpha = max(
                1,
                min(
                    250,
                    self.desktop_background_alpha,
                ),
            )

            sidebar_background = (
                f"rgba(10, 17, 29, {panel_alpha})"
            )

            topbar_background = (
                f"rgba(13, 22, 37, {panel_alpha})"
            )

            model_stage_background = (
                f"rgba(16, 36, 60, {panel_alpha})"
            )

            chat_background = "transparent"

            composer_outer_background = (
                f"rgba(11, 18, 32, {panel_alpha})"
            )

            composer_background = (
                "rgba(16, 26, 43, 245)"
            )

        else:

            background = "#0b1220"

            sidebar_background = "#0a111d"

            topbar_background = "#0d1625"

            model_stage_background = "#10243c"

            chat_background = "#0b1220"

            composer_outer_background = "#0b1220"

            composer_background = "#101a2b"

        # ----------------------------------------------------
        # 个性化聊天气泡
        # ----------------------------------------------------

        user_bubble = (
            self.appearance_settings.get(
                "user_bubble_color",
                "#2D6FE2",
            )
        )

        ai_bubble = (
            self.appearance_settings.get(
                "ai_bubble_color",
                "#17243A",
            )
        )

        user_text = (
            self.appearance_settings.get(
                "user_text_color",
                "#FFFFFF",
            )
        )

        ai_text = (
            self.appearance_settings.get(
                "ai_text_color",
                "#F2F6FD",
            )
        )

        bubble_opacity = (
            self.appearance_settings.get(
                "bubble_opacity",
                98,
            )
        )

        bubble_radius = (
            self.appearance_settings.get(
                "bubble_radius",
                14,
            )
        )

        user_bubble_rgba = (
            self._bubble_rgba(
                user_bubble,
                bubble_opacity,
            )
        )

        ai_bubble_rgba = (
            self._bubble_rgba(
                ai_bubble,
                bubble_opacity,
            )
        )

        # ----------------------------------------------------
        # 聊天背景
        # ----------------------------------------------------

        custom_background = (
            self._chat_background_qss(
                chat_background
            )
        )

        if (
            self.appearance_settings.get(
                "chat_background_mode"
            )
            == "image"
            and "background-image" in custom_background
        ):

            chat_background_qss = (
                custom_background
            )

        else:

            chat_background_qss = (
                f"background: {custom_background};"
            )

        style = """
            QWidget {
                font-family: "Microsoft YaHei", "Segoe UI";
                color: #e7edf8;
                font-size: 14px;
            }

            QMainWindow,
            #central,
            #mainArea {
                background: {background};
                border: none;
            }

            #desktopBackground {
                background: transparent;
                border: none;
            }

            #sidebar {
                background: {sidebar_background};
                border-right: 1px solid #1e2a3b;
            }

            #logo {
                color: #62a8ff;
                font-size: 26px;
            }

            #appTitle {
                font-size: 20px;
                font-weight: 700;
            }

            #topbar {
                background: {topbar_background};
                border-bottom: 1px solid #1e2a3b;
            }

            #topAvatar,
            #modelAvatar,
            #messageAvatar,
            #userAvatar {
                background: #233b63;
                border: 1px solid #5b9dff;
                border-radius: 22px;
                color: #bfe0ff;
                font-weight: 700;
            }

            #topAvatar {
                min-width: 38px;
                max-width: 38px;
                min-height: 38px;
                max-height: 38px;
            }

            #messageAvatar,
            #userAvatar {
                min-width: 38px;
                max-width: 38px;
                min-height: 38px;
                max-height: 38px;
            }

            #topName {
                font-size: 17px;
                font-weight: 700;
            }

            #topSubtitle,
            #settingsSubtitle,
            #modelInfo {
                color: #8392aa;
            }

            #newChatButton,
            #primaryButton {
                background: #3c7ff0;
                border: none;
                border-radius: 10px;
                padding: 11px;
                font-weight: 600;
            }

            #newChatButton:hover,
            #primaryButton:hover {
                background: #4c8cf4;
            }

            #sectionTitle {
                color: #9baac0;
                font-size: 13px;
                font-weight: 700;
                margin-top: 6px;
            }

            QToolButton {
                background: transparent;
                border: none;
                color: #dce5f3;
                padding: 8px;
                border-radius: 8px;
            }

            QToolButton:hover {
                background: #16243a;
            }

            QToolButton:disabled {
                color: #66758d;
            }

            #pluginBadge {
                color: #6f7f96;
                background: #121c2b;
                border-radius: 7px;
                padding: 3px 6px;
                font-size: 10px;
            }

            #sidebarLine {
                color: #1e2a3b;
                background: #1e2a3b;
                max-height: 1px;
            }

            #historyList {
                background: transparent;
                border: none;
                outline: none;
            }

            #historyList::item {
                padding: 9px;
                border-radius: 8px;
                color: #aebbd0;
            }

            #historyList::item:selected {
                background: #16345f;
                color: #68b2ff;
            }

            #modelCard {
                background: #131f31;
                border: 1px solid #243550;
                border-radius: 12px;
            }

            #modelName {
                font-weight: 700;
                font-size: 15px;
            }

            #modelStatus {
                color: #7f8da2;
                font-size: 12px;
            }

            #modelStatusConnected {
                color: #48d597;
                font-size: 12px;
            }

            #sidebarSettings {
                text-align: left;
                padding: 10px;
                color: #aebbd0;
            }

            #modelStage {
                background: {model_stage_background};
                border-bottom: 1px solid #24344c;
            }

            #modelPlaceholder {
                background: qradialgradient(
                    cx: 0.5, cy 0.42,
                    radius: 0.62,
                    stop: 0 #274d77,
                    stop: 0.28 #172e4b,
                    stop: 1 #0c1728
                );
            }

            #modelPlaceholderTitle {
                color: #dff1ff;
                font-size: 38px;
                font-weight: 800;
                letter-spacing: 8px;
            }

            #modelPlaceholderText {
                color: #91a9c4;
                font-size: 13px;
            }

            #stageOverlay {
                background: rgba(7, 13, 23, 210);
                border-top: 1px solid rgba(100, 150, 210, 50);
            }

            #stageStatus {
                color: #a9bad0;
            }

            #chatScroll {
                background: transparent;
                border: none;
            }

            #chatScroll > QWidget {
                background: transparent;
                border: none;
            }

            #chatScroll QWidget#qt_scrollarea_viewport {
                background: transparent;
                border: none;
            }

            #chatScroll QAbstractScrollArea::viewport {
                background: transparent;
                border: none;
            }

            #chatContainer {
                background: transparent;
                border: none;
            }

            #aiBubble,
            #userBubble {
                border-radius: {bubble_radius}px;
                max-width: 700px;
            }

            #aiBubble {
                background: {ai_bubble_rgba};
                border: 1px solid #33496c;
            }

            #userBubble {
                background: {user_bubble_rgba};
                border: 1px solid #4b8cf0;
            }

            #messageName {
                color: #70b7ff;
                font-size: 12px;
                font-weight: 700;
            }

            #messageText {
                color: {ai_text};
            }

            #userBubble #messageText {
                color: {user_text};
            }

            #composerOuter {
                background: {composer_outer_background};
            }

            #composer {
                background: {composer_background};
                border: 1px solid #2b3f5f;
                border-radius: 16px;
            }

            #messageInput {
                background: transparent;
                border: none;
                color: #edf4ff;
                selection-background-color: #316fc9;
            }

            #messageInput:focus {
                border: none;
            }

            #roundToolButton {
                background: #26364f;
                border-radius: 20px;
                min-width: 38px;
                min-height: 38px;
                font-size: 17px;
            }

            #sendButton {
                background: #3c83f4;
                border-radius: 20px;
                min-width: 42px;
                min-height: 42px;
                font-size: 18px;
                font-weight: 700;
            }

            #sendButton:hover {
                background: #5a97f7;
            }

            #attachmentChip {
                background: #16243a;
                border: 1px solid #2c4365;
                border-radius: 9px;
            }

            #settingsTitle {
                font-size: 24px;
                font-weight: 700;
            }

            #settingsCard {
                background: #121d2e;
                border: 1px solid #263750;
                border-radius: 12px;
            }

            QLineEdit,
            QComboBox {
                background: #0d1625;
                border: 1px solid #2a3c58;
                border-radius: 8px;
                padding: 9px;
                color: #eaf1fb;
            }

            QLineEdit:focus,
            QComboBox:focus {
                border: 1px solid #4a8df4;
            }

            #fullscreenStage {
                background: qradialgradient(
                    cx: 0.5, cy 0.42,
                    radius: 0.72,
                    stop: 0 #203e62,
                    stop: 0.4 #112844,
                    stop: 1 #070d16
                );
            }

            #fullscreenTitle {
                color: #dff1ff;
                font-size: 64px;
                font-weight: 800;
                letter-spacing: 14px;
            }

            #fullscreenText {
                color: #9db1c9;
                font-size: 16px;
            }

            #fullscreenClose {
                background: #25354d;
                border: none;
                border-radius: 20px;
                min-width: 44px;
                min-height: 44px;
                font-size: 24px;
            }

            QScrollBar:vertical {
                width: 8px;
                background: transparent;
            }

            QScrollBar::handle:vertical {
                background: #273852;
                border-radius: 4px;
                min-height: 30px;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }

            #sidebarToggle {
                min-width: 36px;
                min-height: 36px;
                font-size: 18px;
            }

            #settingsSectionTitle {
                color: #eaf1fb;
                font-size: 14px;
                font-weight: 700;
            }

            QCheckBox {
                color: #dce5f3;
                spacing: 8px;
            }

            QSlider::groove:horizontal {
                height: 5px;
                background: #26364f;
                border-radius: 2px;
            }

            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                background: #4b8cf5;
                border-radius: 8px;
            }
        """

        style = style.replace(
            "{background}",
            background,
        )

        style = style.replace(
            "{sidebar_background}",
            sidebar_background,
        )

        style = style.replace(
            "{topbar_background}",
            topbar_background,
        )

        style = style.replace(
            "{model_stage_background}",
            model_stage_background,
        )

        style = style.replace(
            "{composer_outer_background}",
            composer_outer_background,
        )

        style = style.replace(
            "{composer_background}",
            composer_background,
        )

        style = style.replace(
            "{bubble_radius}",
            str(
                bubble_radius
            ),
        )

        style = style.replace(
            "{ai_bubble_rgba}",
            ai_bubble_rgba,
        )

        style = style.replace(
            "{user_bubble_rgba}",
            user_bubble_rgba,
        )

        style = style.replace(
            "{ai_text}",
            ai_text,
        )

        style = style.replace(
            "{user_text}",
            user_text,
        )

        self.setStyleSheet(
            style
        )

        self._apply_chat_background_runtime(
            chat_background_qss
        )

    def _apply_chat_background_runtime(
        self,
        background_qss,
    ):
        """
        应用聊天区域的背景。

        修复说明：

        原来图片背景是用 Qt 样式表的
        QAbstractScrollArea::viewport 选择器画的，
        但 Qt 并不支持这个选择器，
        整条规则会被静默忽略，
        于是出现「选了自定义背景图片，背景板上却什么都没有」。

        而且即使选择器有效，样式表也不支持 background-size，
        图片只会按原始像素画在正中间，
        既铺不满，也不会随窗口大小变化。

        现在改成由 _paint_chat_background() 自己绘制：
        等比缩放到铺满整个聊天区域，多余部分从中心裁掉，
        窗口尺寸变化时自动重画。

        聊天区域原来的布局和滚动结构完全没有改动。
        """

        mode = (
            self.appearance_settings.get(
                "chat_background_mode",
                "default",
            )
        )

        image_path = ""

        if mode == "image":

            image_path = (
                self.appearance_settings.get(
                    "chat_background_image",
                    "",
                )
                or ""
            )

        self._set_chat_background_image(
            image_path
        )

        # 无论有没有背景图，
        # 聊天滚动区本身都保持透明，
        # 这样画在 viewport 上的背景才不会被它盖住。
        self.chat_scroll.setStyleSheet(
            """
            QScrollArea {
                background: transparent;
                border: none;
            }

            QScrollArea > QWidget {
                background: transparent;
                border: none;
            }
            """
        )

        self.chat_container.setStyleSheet(
            "background: transparent; border: none;"
        )

    def _set_chat_background_image(
        self,
        image_path,
    ):
        """
        载入或清除自定义聊天背景图。

        只在图片变化时读一次文件并缓存原图，
        之后每次重绘都直接用缓存，不会反复读磁盘。

        图片不存在、损坏或读取失败时，
        只是没有背景图，不会影响界面继续使用。
        """

        image_path = str(
            image_path or ""
        ).strip()

        cache_key = image_path

        if image_path:

            try:

                stat = Path(
                    image_path
                ).stat()

                cache_key = (
                    f"{image_path}"
                    f"|{stat.st_mtime_ns}"
                    f"|{stat.st_size}"
                )

            except Exception:

                cache_key = image_path

        if cache_key == self.chat_background_key:

            return

        self.chat_background_key = cache_key

        self.chat_background_pixmap = QPixmap()

        # 原图换了，裁切缓存也要一起作废。
        self.chat_background_scaled = QPixmap()

        self.chat_background_scaled_key = None

        if image_path:

            try:

                if Path(image_path).exists():

                    pixmap = QPixmap(
                        image_path
                    )

                    if not pixmap.isNull():

                        self.chat_background_pixmap = (
                            pixmap
                        )

            except Exception:

                self.chat_background_pixmap = QPixmap()

        try:

            self.chat_scroll.viewport().update()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def _paint_chat_background(
        self,
    ):
        """
        把自定义背景图画到聊天滚动区的 viewport 上。

        使用「等比铺满 + 中心裁切」：
        图片不会被拉伸变形，也不会留下空白边。
        """

        if self.chat_background_pixmap.isNull():

            return

        viewport = self.chat_scroll.viewport()

        width = viewport.width()
        height = viewport.height()

        if width <= 0 or height <= 0:

            return

        opacity = clamp_int(
            self.appearance_settings.get(
                "chat_background_opacity",
                100,
            ),
            0,
            100,
            100,
        )

        if opacity <= 0:

            return

        # 按当前尺寸裁切，并缓存结果。
        scaled_key = (
            self.chat_background_key,
            width,
            height,
        )

        if (
            self.chat_background_scaled.isNull()
            or self.chat_background_scaled_key
            != scaled_key
        ):

            self.chat_background_scaled = (
                scale_pixmap_to_fill(
                    self.chat_background_pixmap,
                    width,
                    height,
                )
            )

            self.chat_background_scaled_key = (
                scaled_key
            )

        scaled = self.chat_background_scaled

        if scaled.isNull():

            return

        try:

            painter = QPainter(
                viewport
            )

            # 必须先把上一帧擦干净。
            #
            # 聊天区域的控件都是透明的、也不会自己清背景，
            # 如果不清就直接画半透明图片，
            # 透明度会一层层叠加上去，
            # 表现为「调了透明度却完全没有变化」。
            painter.setCompositionMode(
                QPainter.CompositionMode_Source
            )

            painter.fillRect(
                0,
                0,
                width,
                height,
                Qt.transparent,
            )

            painter.setCompositionMode(
                QPainter.CompositionMode_SourceOver
            )

            painter.setOpacity(
                opacity / 100.0
            )

            painter.drawPixmap(
                0,
                0,
                scaled,
            )

            painter.end()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()


    # ========================================================
    # 桌面小人物（V0.1）
    # ========================================================

    def changeEvent(
        self,
        event,
    ):
        """
        根据主窗口的最小化 / 恢复状态显示或隐藏桌面小人物。

        只处理 WindowStateChange，其余事件原样交给父类。
        这里不修改任何窗口属性，也不触碰聊天数据。
        """

        try:

            super().changeEvent(
                event
            )

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        try:

            if event.type() != QEvent.WindowStateChange:

                return

            if getattr(
                self,
                "_mascot_shutting_down",
                False,
            ):

                return

            if self.isMinimized():

                self._show_mascot()

            else:

                self._hide_mascot()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def _show_mascot(
        self,
    ):
        """
        显示桌面小人物。

        同一时间只允许存在一个实例：
        已经有实例就直接显示，不再创建第二个。
        """

        if getattr(
            self,
            "_mascot_shutting_down",
            False,
        ):

            return

        if getattr(
            self,
            "_mascot_busy",
            False,
        ):

            return

        self._mascot_busy = True

        try:

            if MascotWindow is None:

                return

            existing = getattr(
                self,
                "mascot_window",
                None,
            )

            if existing is not None:

                try:

                    existing.show()
                    existing.raise_()

                    return

                except RuntimeError:

                    # C++ 对象已被销毁，走下面重新创建
                    self.mascot_window = None

            window = create_mascot()

            if window is None:

                return

            window.clicked.connect(
                self._restore_from_mascot
            )

            window.request_open_main.connect(
                self._restore_from_mascot
            )

            window.request_quit.connect(
                self.close
            )

            self.mascot_window = window

            window.show_at_default_position()

            log_lines(
                "[小人物] 已显示（主窗口最小化）"
            )

        except Exception as exc:

            log_lines(
                f"[小人物] 显示失败（已忽略）：{exc!r}"
            )

        finally:

            self._mascot_busy = False

    def _hide_mascot(
        self,
    ):
        """隐藏桌面小人物（不销毁，便于下次快速显示）。"""

        window = getattr(
            self,
            "mascot_window",
            None,
        )

        if window is None:

            return

        try:

            window.hide()

        except RuntimeError:

            self.mascot_window = None

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def _destroy_mascot(
        self,
    ):
        """销毁桌面小人物窗口并断开引用。"""

        window = getattr(
            self,
            "mascot_window",
            None,
        )

        self.mascot_window = None

        if window is None:

            return

        try:

            window.hide()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        try:

            window.close()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        try:

            window.deleteLater()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

    def _restore_from_mascot(
        self,
    ):
        """
        点击小人物后：先隐藏小人物，再恢复主窗口。

        顺序很重要 —— 小人物是置顶窗口，
        如果先恢复主窗口，主窗口会被压在下面。
        """

        try:

            self._hide_mascot()

            self.showNormal()

            self.raise_()

            self.activateWindow()

            log_lines(
                "[小人物] 已恢复主窗口"
            )

        except Exception as exc:

            log_lines(
                f"[小人物] 恢复主窗口失败：{exc!r}"
            )

    def _stop_background_threads(
        self,
    ):
        """
        退出时对后台线程做有界收尾，并如实记录实际状态。

        重要限制（不夸大）：
        * quit() 只请求结束线程的事件循环，
          无法中断 run() 里已经阻塞的调用
          （网络请求 / 麦克风）。
        * 所以 AI 请求或语音识别进行中时，
          线程很可能在 1.5 秒内停不下来。
        * 本方法不调用 terminate()（强杀可能损坏数据），
          也不把 setParent(None) 当作线程已停止的证明。

        返回：每个线程的状态记录列表。
        """

        report = []

        targets = []

        try:

            for index, pair in enumerate(
                list(
                    getattr(
                        self,
                        "worker_threads",
                        [],
                    )
                )
            ):

                if isinstance(pair, (tuple, list)) and len(pair) >= 2:

                    targets.append(
                        (f"worker#{index}", pair[0])
                    )

        except Exception as exc:

            print(f'[Desktop Transparency] Error collecting worker_threads: {exc}')
            import traceback
            traceback.print_exc()

        # 感知回复线程也纳入关闭流程。
        try:

            for index, pair in enumerate(
                list(
                    getattr(
                        self,
                        "awareness_reply_threads",
                        [],
                    )
                )
            ):

                if isinstance(pair, (tuple, list)) and len(pair) >= 2:

                    targets.append(
                        (f"awareness_reply#{index}", pair[0])
                    )

        except Exception as exc:

            print(f'[Desktop Transparency] Error collecting awareness_reply_threads: {exc}')
            import traceback
            traceback.print_exc()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        try:

            if getattr(self, "speech_thread", None) is not None:

                targets.append(
                    ("speech", self.speech_thread)
                )

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        wait_ms = 1500

        for name, thread in targets:

            entry = {
                "name": name,
                "was_running": None,
                "quit_requested": False,
                "stopped_within_timeout": None,
                "still_running": None,
                "detached": False,
                "error": None,
            }

            try:

                if thread is None:

                    entry["error"] = "引用为空"
                    report.append(entry)
                    continue

                entry["was_running"] = bool(
                    thread.isRunning()
                )

                if not entry["was_running"]:

                    entry["stopped_within_timeout"] = True
                    entry["still_running"] = False
                    report.append(entry)
                    continue

                thread.quit()

                entry["quit_requested"] = True

                entry["stopped_within_timeout"] = bool(
                    thread.wait(wait_ms)
                )

                # 以 isRunning() 的实测值作为唯一判据
                still = bool(
                    thread.isRunning()
                )

                entry["still_running"] = still

                if still:

                    # 只解除父子关系，避免 Qt 在销毁主窗口时
                    # 连带销毁运行中的线程而触发致命错误。
                    # 这不能证明线程已经停止。
                    try:

                        thread.setParent(None)

                        entry["detached"] = True

                    except Exception:

                        pass

                    # 保住 Python 引用，避免对象被回收
                    try:

                        app = QApplication.instance()

                        if app is not None:

                            orphans = getattr(
                                app,
                                "_arona_orphan_threads",
                                None,
                            )

                            if orphans is None:

                                orphans = []

                                app._arona_orphan_threads = orphans

                            orphans.append(thread)

                    except Exception:

                        pass

            except RuntimeError as exc:

                entry["error"] = f"对象已析构: {exc!r}"

            except Exception as exc:

                entry["error"] = repr(exc)

            report.append(entry)

        self._thread_shutdown_report = report

        return report

    def closeEvent(
        self,
        event,
    ):
        """
        关闭主窗口 = 退出整个程序。

        顺序：
          1. 置退出标志，阻止小人物被再次创建
          2. 销毁小人物窗口
          3. 有界收尾后台线程并记录真实状态
          4. 放行关闭事件（保持原有退出行为）
        """

        self._mascot_shutting_down = True

        try:

            self._destroy_mascot()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        report = []

        # V0.3 先停止桌面自主感知
        self._stop_desktop_awareness()

        # R12.3-07 M3：收尾图片生成后台线程（G9）
        try:
            self._stop_image_gen_threads()
        except Exception:
            pass

        try:

            report = self._stop_background_threads()

        except Exception as exc:

            report = [{"error": repr(exc)}]

        try:

            stuck = [
                e for e in report
                if e.get("still_running")
            ]

            lines = [
                "=" * 60,
                "[退出] 后台线程收尾报告",
                f"  等待上限: 1500 ms    线程数: {len(report)}",
            ]

            if not report:

                lines.append("  没有需要收尾的线程")

            for e in report:

                lines.append(
                    "  {name}: was_running={was_running} "
                    "quit_requested={quit_requested} "
                    "stopped_within_timeout={stopped_within_timeout} "
                    "still_running={still_running} "
                    "detached={detached} error={error}".format(
                        name=e.get("name", "-"),
                        was_running=e.get("was_running"),
                        quit_requested=e.get("quit_requested"),
                        stopped_within_timeout=e.get("stopped_within_timeout"),
                        still_running=e.get("still_running"),
                        detached=e.get("detached"),
                        error=e.get("error"),
                    )
                )

            if stuck:

                lines.append(
                    f"  警告: {len(stuck)} 个线程未能在 1.5 秒内停止，"
                    "进程可能无法立即完全退出。"
                )

                lines.append(
                    "  已保留引用并解除父子关系；未调用 terminate()。"
                )

            lines.append("=" * 60)

            log_lines(
                lines
            )

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()

        try:

            event.accept()

        except Exception as exc:

            print(f'[Desktop Transparency] Error in _set_windows_desktop_passthrough: {exc}')
            import traceback
            traceback.print_exc()


# ============================================================
# 程序入口
# ============================================================

def start_arona():

    print(
        "================================"
    )

    print(
        "正在启动新版 AronaClient"
    )

    print(
        "================================"
    )

    print(
        "本地数据目录：",
        DATA_DIR,
    )

    print(
        "本地工作空间：",
        WORKSPACE_DIR,
    )

    app = QApplication.instance()

    if app is None:

        app = QApplication(
            sys.argv
        )

    print(
        "QApplication 创建完成"
    )

    try:

        window = AronaUI()

        print(
            "AronaUI 创建成功：",
            type(window),
        )

    except Exception as exc:

        print(
            "================================"
        )

        print(
            "AronaUI 创建失败"
        )

        print(
            "================================"
        )

        print(
            exc
        )

        import traceback

        traceback.print_exc()

        return 1

    app._arona_window = window

    def show_window():

        print(
            "准备显示 AronaClient 窗口"
        )

        try:

            window.setWindowState(
                Qt.WindowNoState
            )

            window.show()

            window.raise_()

            window.activateWindow()

            print(
                "AronaClient 窗口已 show"
            )

            print(
                "等待 Qt 事件循环……"
            )

        except Exception as exc:

            print(
                "================================"
            )

            print(
                "显示窗口时发生错误"
            )

            print(
                "================================"
            )

            print(
                exc
            )

            import traceback

            traceback.print_exc()

    QTimer.singleShot(
        100,
        show_window,
    )

    return app.exec()


if __name__ == "__main__":

    sys.exit(
        start_arona()
    )