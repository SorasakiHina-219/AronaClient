# mascot_window.py
# AronaClient V0.1
# 桌面小人物：独立的透明桌面角色窗口
#
# 设计要点：
# 1. 完全独立于 ui.py，ui.py 只做最小接线。
# 2. 窗口是「无父对象的顶层窗口」——
#    如果以主窗口为 parent，它在 Windows 上属于被拥有窗口，
#    主窗口最小化时会被一起隐藏，功能就废了。
# 3. WA_TranslucentBackground 等属性在 show() 之前设好
#    （显示之后再改，Qt 不会重建原生窗口，容易出现重绘异常）。
# 4. 素材三级回退：data/mascot/arona.png → arona.jpg → 程序内绘制。
#    三级全部失败也不会抛异常，不会导致客户端启动失败。
# 5. 本模块不联网、不截屏、不控制鼠标键盘、不写除日志外的任何文件。

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget


# V0.1 桌面小人物：真实 3D 模型（阶段 B）。
#
# 用 try 包住：万一 3D 模块或模型不可用，
# 就退回原来的「程序内绘制占位角色」，不影响任何既有功能。
try:

    from mascot_3d import MascotLoader

except Exception as _mascot3d_error:

    MascotLoader = None

    print(
        "3D 角色模块加载失败，将使用占位角色：",
        _mascot3d_error,
    )


# ============================================================
# 路径与常量
# ============================================================

APP_DIR = Path(__file__).resolve().parent

DATA_DIR = APP_DIR / "data"
MASCOT_DIR = DATA_DIR / "mascot"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "mascot_shutdown.log"

# 角色默认高度（像素）
DEFAULT_HEIGHT = 220

# 视作「单击」而不是「拖动」的位移阈值
CLICK_TOLERANCE = 4

# 素材优先级
SKIN_CANDIDATES = ("arona.png", "arona.jpg", "arona.jpeg")

# 日志文件上限，超过就截断保留后半段，避免长期运行无限增长
MAX_LOG_BYTES = 256 * 1024

# 角色离屏幕右下角的边距
SCREEN_MARGIN = 24

# 真实 3D 模型加载期间，窗口先占一个透明的临时尺寸。
# 期间不显示任何人物；模型就绪后会按实际比例调整尺寸。
PROVISIONAL_W = 110
PROVISIONAL_H = 320


# ============================================================
# 日志：控制台 + 文件（追加、限长、绝不抛异常）
# ============================================================

def log_lines(lines):
    """
    把若干行文本同时输出到控制台和日志文件。

    规则：
    * 追加模式，不覆盖历史记录。
    * 文件超过 MAX_LOG_BYTES 时，只保留后半段。
    * 任何写入失败都只回退到控制台，绝不抛出异常，
      以免阻断程序退出流程。
    * 调用方负责不要传敏感信息进来（本模块自身不记录任何密钥或聊天内容）。
    """

    if isinstance(lines, str):
        lines = [lines]

    text = "\n".join(str(x) for x in lines)

    # 1) 控制台（失败也不管）
    try:
        print(text)
    except Exception:
        pass

    # 2) 文件
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            try:
                tail = LOG_FILE.read_bytes()[-MAX_LOG_BYTES // 2:]
                LOG_FILE.write_bytes(
                    b"...(older entries trimmed)...\n" + tail
                )
            except Exception:
                pass

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for line in str(text).splitlines():
                f.write(f"[{stamp}] {line}\n")

        return True

    except Exception as exc:
        # 回退：写不进文件也要让用户看到
        try:
            print(f"[mascot] 日志写入失败（已忽略，不影响退出）：{exc}")
        except Exception:
            pass
        return False


# ============================================================
# 角色素材
# ============================================================

class MascotSkin:
    """
    角色外观加载。

    优先级：
        1. data/mascot/arona.png   （未来正式透明立绘放这里）
        2. data/mascot/arona.jpg   （备用图片位，会套圆角蒙版）
        3. 程序内 QPainter 绘制的简笔 Arona 风格角色

    替换正式立绘时只需要往 data/mascot/ 放文件，
    不需要修改本模块或 ui.py 的任何代码。
    """

    @staticmethod
    def find_source():
        """返回第一个可用的素材路径；都没有则返回 None。"""

        for name in SKIN_CANDIDATES:

            try:
                path = MASCOT_DIR / name

                if path.is_file() and path.stat().st_size > 0:
                    return path

            except Exception:
                continue

        return None

    @staticmethod
    def load(target_height=DEFAULT_HEIGHT, device_pixel_ratio=1.0):
        """加载角色图像。任何失败都会回退到内置绘制，不抛异常。"""

        height = max(48, int(target_height))
        dpr = device_pixel_ratio if device_pixel_ratio and device_pixel_ratio > 0 else 1.0
        px_height = max(1, int(round(height * dpr)))

        source = MascotSkin.find_source()

        if source is not None:

            try:

                raw = QPixmap(str(source))

                if not raw.isNull() and raw.width() > 0 and raw.height() > 0:

                    scaled = raw.scaledToHeight(
                        px_height,
                        Qt.SmoothTransformation,
                    )

                    has_alpha = raw.hasAlphaChannel()

                    if not has_alpha:
                        scaled = MascotSkin._rounded(scaled)

                    scaled.setDevicePixelRatio(dpr)

                    return scaled

            except Exception:
                pass

        # 找不到任何素材时不再回退到自制占位角色。
        # 真实 Arona 由 mascot_3d 的 OpenGL 部件渲染；
        # 这里返回空图，调用方不会绘制任何东西。
        return QPixmap()

    @staticmethod
    def _rounded(pixmap, radius_ratio=0.18):
        """给没有透明通道的图片套圆角蒙版，让四角透明。"""

        try:

            width = pixmap.width()
            height = pixmap.height()

            if width <= 0 or height <= 0:
                return pixmap

            result = QPixmap(width, height)
            result.fill(Qt.transparent)

            painter = QPainter(result)
            painter.setRenderHint(QPainter.Antialiasing, True)

            radius = max(4.0, min(width, height) * radius_ratio)

            path_rect = QRectF(0, 0, width, height)

            from PySide6.QtGui import QPainterPath

            path = QPainterPath()
            path.addRoundedRect(path_rect, radius, radius)

            painter.setClipPath(path)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()

            return result

        except Exception:
            return pixmap

    @staticmethod
    def draw_builtin(height=DEFAULT_HEIGHT):
        """
        程序内绘制的简笔 Arona 风格占位角色。

        特征：头顶光环、白色头发、蓝紫色服装、底部柔和阴影。
        全部用 QPainter 基础图形绘制，不依赖任何外部文件。
        """

        h = max(64, int(height))
        w = max(48, int(h * 0.72))

        pixmap = QPixmap(w, h)
        pixmap.fill(Qt.transparent)

        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing, True)

        cx = w / 2.0

        try:

            # ---------------- 底部柔和阴影 ----------------
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 55))
            p.drawEllipse(
                QRectF(w * 0.16, h * 0.905, w * 0.68, h * 0.075)
            )

            # ---------------- 服装（蓝紫裙） ----------------
            grad = QLinearGradient(0, h * 0.46, 0, h * 0.92)
            grad.setColorAt(0.0, QColor(74, 95, 168))
            grad.setColorAt(1.0, QColor(42, 53, 104))

            p.setBrush(QBrush(grad))
            p.setPen(Qt.NoPen)

            dress = QRectF(
                cx - w * 0.30,
                h * 0.455,
                w * 0.60,
                h * 0.455,
            )
            p.drawRoundedRect(dress, w * 0.16, w * 0.16)

            # 领口亮色
            p.setBrush(QColor(232, 240, 255))
            p.drawEllipse(
                QRectF(cx - w * 0.085, h * 0.445, w * 0.17, h * 0.05)
            )

            # ---------------- 头发（后发） ----------------
            p.setBrush(QColor(233, 242, 255))
            p.setPen(Qt.NoPen)
            p.drawEllipse(
                QRectF(cx - w * 0.30, h * 0.155, w * 0.60, h * 0.34)
            )

            # ---------------- 头部 ----------------
            p.setBrush(QColor(251, 233, 223))
            p.drawEllipse(
                QRectF(cx - w * 0.215, h * 0.185, w * 0.43, h * 0.30)
            )

            # ---------------- 刘海 ----------------
            p.setBrush(QColor(245, 250, 255))
            p.drawEllipse(
                QRectF(cx - w * 0.245, h * 0.155, w * 0.49, h * 0.185)
            )

            # 两侧发束
            p.setBrush(QColor(233, 242, 255))
            p.drawEllipse(
                QRectF(cx - w * 0.315, h * 0.30, w * 0.12, h * 0.26)
            )
            p.drawEllipse(
                QRectF(cx + w * 0.195, h * 0.30, w * 0.12, h * 0.26)
            )

            # ---------------- 眼睛 ----------------
            p.setBrush(QColor(107, 79, 216))
            p.setPen(Qt.NoPen)
            p.drawEllipse(
                QRectF(cx - w * 0.135, h * 0.315, w * 0.085, h * 0.055)
            )
            p.drawEllipse(
                QRectF(cx + w * 0.050, h * 0.315, w * 0.085, h * 0.055)
            )

            # ---------------- 腮红 ----------------
            p.setBrush(QColor(255, 150, 150, 70))
            p.drawEllipse(
                QRectF(cx - w * 0.215, h * 0.375, w * 0.085, h * 0.032)
            )
            p.drawEllipse(
                QRectF(cx + w * 0.130, h * 0.375, w * 0.085, h * 0.032)
            )

            # ---------------- 头顶光环 ----------------
            halo_rect = QRectF(
                cx - w * 0.20,
                h * 0.055,
                w * 0.40,
                h * 0.075,
            )

            # 外发光
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(143, 211, 255, 90), max(3.0, h * 0.022)))
            p.drawEllipse(halo_rect)

            # 内圈
            p.setPen(QPen(QColor(190, 232, 255, 220), max(2.0, h * 0.012)))
            p.drawEllipse(halo_rect)

        except Exception:
            # 绘制失败也不让窗口创建失败
            pass

        finally:
            p.end()

        return pixmap


# ============================================================
# 角色窗口
# ============================================================

class MascotWindow(QWidget):
    """
    桌面小人物窗口。

    * 无边框、透明背景、默认置顶
    * 左键拖动；位移很小则视为单击
    * 右键弹出菜单
    * 不进入任务栏（Qt.Tool）
    * WA_QuitOnClose=False：它不会单独让程序保持存活，
      退出行为与「没有小人物时」完全一致
    """

    clicked = Signal()
    request_open_main = Signal()
    request_quit = Signal()

    def __init__(self, parent=None):

        # 关键：parent 必须为 None，做成独立顶层窗口。
        super().__init__(None)

        self._pressed_at = QPoint()
        self._drag_offset = QPoint()
        self._dragging = False
        self._moved = 0
        self._pixmap = QPixmap()

        # ---- 3D 角色（阶段 B）----
        # _loader 负责后台加载模型；_gl_view 是加载成功后创建的 GL 子部件。
        # 两者都为 None 时，行为与之前完全一致（画 _pixmap）。
        self._loader = None
        self._gl_view = None
        self._loading = False

        # ---- 窗口属性：必须在 show() 之前设好 ----
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setAutoFillBackground(False)

        self.setWindowTitle("Arona")
        self.setToolTip("Arona 在桌面上陪着你 —— 单击我打开主窗口")

        self.apply_skin()

    # --------------------------------------------------------

    def apply_skin(self, target_height=DEFAULT_HEIGHT):
        """
        只确定窗口尺寸。

        这里**不再加载或绘制任何自制占位角色 / 静态图片**：
        桌面上的人物一律由 mascot_3d 的真实 Arona 3D 模型渲染。
        模型未就绪时窗口保持完全透明，不显示任何人物。
        """

        # 明确不使用任何贴图（保留属性以免影响既有调用方）
        self._pixmap = QPixmap()

        self.setFixedSize(
            PROVISIONAL_W,
            PROVISIONAL_H,
        )

        self.update()

    # --------------------------------------------------------

    # --------------------------------------------------------
    # 3D 角色（阶段 B）
    # --------------------------------------------------------

    def enable_3d(self):
        """
        启动后台加载真实 Arona 模型。

        * 加载在工作线程里做，主窗口和小人物都保持可响应
        * 失败时退回占位角色，不抛异常、不影响其他功能
        """

        if MascotLoader is None or self._loader is not None or self._gl_view is not None:

            return False

        try:

            self._loading = True

            loader = MascotLoader(self)

            loader.ready.connect(
                self._on_3d_ready
            )

            loader.failed.connect(
                self._on_3d_failed
            )

            self._loader = loader

            loader.start()

            return True

        except Exception as exc:

            self._loading = False

            log_lines(
                f"[小人物] 3D 加载启动失败（改用占位角色）：{exc!r}"
            )

            return False

    def _on_3d_ready(
        self,
        view,
    ):
        """后台加载成功：把 GL 部件挂进窗口。"""

        try:

            self._loading = False

            if view is None:

                return

            self._gl_view = view

            # 关键：窗口尺寸要跟随 3D 部件，而不是沿用占位贴图的尺寸。
            # 之前 GL 部件是 79x240、窗口却是 158x220，导致模型只画在左边
            # 一条里、底部还被裁掉，看起来又小又糊。
            vw = max(24, int(view.width()))
            vh = max(24, int(view.height()))

            view.setGeometry(
                0,
                0,
                vw,
                vh,
            )

            self.setFixedSize(
                vw,
                vh,
            )

            # 尺寸变大后重新夹回屏幕可用区域，
            # 否则窗口会向下长、压到任务栏上。
            try:

                _app = QApplication.instance()

                _scr = _app.primaryScreen() if _app is not None else None

                if _scr is not None:

                    _area = _scr.availableGeometry()

                    _x = min(
                        max(self.x(), _area.left()),
                        _area.right() - vw,
                    )

                    _y = min(
                        max(self.y(), _area.top()),
                        _area.bottom() - vh,
                    )

                    self.move(int(_x), int(_y))

            except Exception:

                pass

            view.show()
            view.raise_()

            self.update()

            log_lines(
                "[小人物] 真实 3D 模型已就绪"
            )

        except Exception as exc:

            log_lines(
                f"[小人物] 3D 部件接入失败（改用占位角色）：{exc!r}"
            )

            self._gl_view = None

    def _on_3d_failed(
        self,
        message,
    ):
        """后台加载失败：如实记录，继续用占位角色。"""

        self._loading = False

        log_lines(
            f"[小人物] 3D 模型加载失败，继续使用占位角色：{message}"
        )

    def shutdown_3d(
        self,
    ):
        """取消未完成的加载，并释放 GL 资源。退出时调用。"""

        loader = self._loader

        self._loader = None

        self._loading = False

        if loader is not None:

            try:

                loader.shutdown()

            except Exception:

                pass

        view = self._gl_view

        self._gl_view = None

        if view is not None:

            try:

                view.cleanup()

            except Exception:

                pass

            try:

                view.hide()
                view.deleteLater()

            except Exception:

                pass

    def closeEvent(
        self,
        event,
    ):
        """关闭时释放 3D 资源与后台加载线程。"""

        try:

            self.shutdown_3d()

        except Exception:

            pass

        super().closeEvent(
            event
        )

    def resizeEvent(
        self,
        event,
    ):
        """让 GL 子部件跟随窗口尺寸。"""

        try:

            if self._gl_view is not None:

                self._gl_view.setGeometry(
                    0,
                    0,
                    self.width(),
                    self.height(),
                )

        except Exception:

            pass

        super().resizeEvent(
            event
        )

    def show_at_default_position(self):
        """出现在主屏幕右下角，并夹紧在可用区域内。"""

        try:
            app = QApplication.instance()
            screen = app.primaryScreen() if app is not None else None

            if screen is not None:

                area = screen.availableGeometry()

                x = area.right() - self.width() - SCREEN_MARGIN
                y = area.bottom() - self.height() - SCREEN_MARGIN

                x = max(area.left(), min(x, area.right() - self.width()))
                y = max(area.top(), min(y, area.bottom() - self.height()))

                self.move(int(x), int(y))

        except Exception:
            pass

        self.show()
        self.raise_()

    # --------------------------------------------------------
    # 绘制
    # --------------------------------------------------------

    def paintEvent(self, event):

        # 窗口本身始终保持完全透明：
        # 人物只由 mascot_3d 的真实 Arona 3D 部件绘制。
        # 模型未就绪或加载失败时，这里什么都不画 —— 桌面直接透出来，
        # 绝不显示任何自制占位角色。
        event.accept()

    # --------------------------------------------------------
    # 鼠标
    # --------------------------------------------------------

    def mousePressEvent(self, event):

        if event.button() == Qt.LeftButton:

            self._pressed_at = event.globalPosition().toPoint()
            self._drag_offset = self._pressed_at - self.frameGeometry().topLeft()
            self._dragging = True
            self._moved = 0

            event.accept()

            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):

        if self._dragging:

            now = event.globalPosition().toPoint()

            delta = now - self._pressed_at
            self._moved = max(self._moved, abs(delta.x()) + abs(delta.y()))

            self.move(now - self._drag_offset)

            event.accept()

            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):

        if event.button() == Qt.LeftButton and self._dragging:

            self._dragging = False

            if self._moved <= CLICK_TOLERANCE:
                self.clicked.emit()

            event.accept()

            return

        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):

        try:
            menu = QMenu(self)
            menu.setStyleSheet(
                """
                QMenu {
                    background: #0d1625;
                    color: #F5F7FA;
                    border: 1px solid #33496C;
                    padding: 4px;
                }
                QMenu::item {
                    padding: 6px 18px;
                }
                QMenu::item:selected {
                    background: #2D6FE2;
                }
                """
            )

            act_open = menu.addAction("打开主窗口")
            menu.addSeparator()
            act_quit = menu.addAction("退出程序")

            chosen = menu.exec(event.globalPos())

            if chosen is act_open:
                self.request_open_main.emit()
            elif chosen is act_quit:
                self.request_quit.emit()

        except Exception:
            pass

        event.accept()


# ============================================================
# 唯一创建入口
# ============================================================

def create_mascot(target_height=DEFAULT_HEIGHT):
    """
    创建桌面小人物窗口。

    任何失败都返回 None（并记录一行日志），
    由调用方决定降级，绝不让主程序崩掉。
    """

    try:

        window = MascotWindow()

        if window.width() <= 1 or window.height() <= 1:
            log_lines("[小人物] 角色尺寸异常，放弃创建")
            return None

        # 后台加载真实 3D 模型（阶段 B）。
        # 失败会自动退回占位角色，不影响窗口本身的可用性。
        try:

            window.enable_3d()

        except Exception as exc:

            log_lines(
                f"[小人物] 3D 初始化失败（继续用占位角色）：{exc!r}"
            )

        return window

    except Exception as exc:

        log_lines(f"[小人物] 创建失败（已忽略，主程序继续运行）：{exc!r}")

        return None
