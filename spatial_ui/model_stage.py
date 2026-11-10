"""
spatial_ui.model_stage — Arona 空间角色舞台（背景 + 模型帧软件合成 + 状态提示）。

图层职责（严格隔离）：
  1. 背景层：本组件自绘 SpaceBackground 缓存的对应区域（与主背景逐像素一致），
     保证模型背后**永远**是蓝紫渐变，不会露出深色 backing store。
  2. 模型层：`AronaModelRuntime` 离屏渲染出的带 alpha 帧（QImage），
     用 QPainter 叠加在渐变上（软件合成，alpha 正确，QOpenGLWidget 不参与窗口合成）。
  3. UI 层：由主窗口负责，本组件不绘制任何装饰。
  4. 装饰层：无（房间/窗景/几何人形均已删除）。

交互：左键拖拽旋转、滚轮缩放、双击（全屏信号）。
"""

import warnings
warnings.filterwarnings("ignore")

from pathlib import Path

from PySide6.QtCore import Qt, QRect, QEvent, QPoint, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPixmap, QPen
from PySide6.QtWidgets import QWidget

from spatial_ui.styles import PERFORMANCE_MODES

_STAGE_IMAGE_CANDIDATES = (
    Path("data") / "stage" / "arona.png",
    Path("data") / "stage" / "arona.jpg",
)


class SpatialModelStage(QWidget):
    """Arona 3D 模型显示舞台。"""

    toggled = Signal(bool)
    fullscreen_requested = Signal()
    clicked = Signal()
    runtime_state_changed = Signal(str)

    def __init__(
        self,
        parent=None,
        *,
        performance_mode: str = "medium",
        enable_runtime: bool = True,
    ):
        super().__init__(parent)

        self._perf_mode = performance_mode
        self._perf = PERFORMANCE_MODES.get(
            performance_mode, PERFORMANCE_MODES["medium"]
        )

        self.setObjectName("spatialModelStage")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setMinimumHeight(240)
        self.setMouseTracking(True)

        self._status_text = "Arona 正在等待你的消息～"
        self._char_pixmap = QPixmap()
        self._load_optional_image()

        # 模型帧（带 alpha 的软件合成结果）
        self._model_pixmap: QPixmap = QPixmap()
        self._model_rect = QRect()

        self._runtime = None
        self._runtime_state = "off"
        self._runtime_message = ""
        self._runtime_wanted = bool(enable_runtime)

        self._drag_last = None

    # ── 可选静态素材 ──────────────────────────────────────

    def _load_optional_image(self):
        for p in _STAGE_IMAGE_CANDIDATES:
            try:
                if p.exists():
                    pm = QPixmap(str(p))
                    if not pm.isNull():
                        self._char_pixmap = pm
                        return
            except Exception:
                continue
        self._char_pixmap = QPixmap()

    # ── 3D 运行时 ─────────────────────────────────────────

    def enable_runtime(self, enabled: bool = True):
        self._runtime_wanted = bool(enabled)
        if enabled:
            self._start_runtime()
        else:
            self._stop_runtime(disable=True)

    def _start_runtime(self):
        if self._runtime_state in ("loading", "ready"):
            return
        try:
            from spatial_ui.model_runtime import AronaModelRuntime
        except Exception as exc:
            self._runtime_state = "failed"
            self._runtime_message = f"3D 运行时模块不可用：{exc}"
            self._emit_state()
            self.update()
            return

        if self._runtime is None:
            self._runtime = AronaModelRuntime(
                self, performance_mode=self._perf_mode
            )
            self._runtime.progress.connect(self._on_runtime_progress)
            self._runtime.ready.connect(self._on_runtime_ready)
            self._runtime.failed.connect(self._on_runtime_failed)
            self._runtime.frame_ready.connect(self._on_frame_ready)

        self._runtime_state = "loading"
        self._runtime_message = "正在加载 Arona 模型…"
        self._emit_state()
        self.update()
        self._runtime.layout_view(self._view_rect())
        self._runtime.start()

    def _stop_runtime(self, disable: bool = False):
        if self._runtime is not None:
            try:
                if disable:
                    self._runtime.disable()
                else:
                    self._runtime.shutdown()
            except Exception:
                pass
        self._runtime_state = "disabled" if disable else "off"
        self._runtime_message = ""
        self._model_pixmap = QPixmap()
        self._model_rect = QRect()
        self._emit_state()
        self.update()

    def _emit_state(self):
        try:
            self.runtime_state_changed.emit(self._runtime_state)
        except Exception:
            pass

    def _on_runtime_progress(self, msg: str):
        self._runtime_message = msg
        if self._runtime_state == "loading":
            self.update()

    def _on_runtime_ready(self):
        self._runtime_state = "ready"
        self._runtime_message = ""
        self._emit_state()
        self._sync_view()

    def _on_runtime_failed(self, msg: str):
        self._runtime_state = "failed"
        self._runtime_message = str(msg)
        self._model_pixmap = QPixmap()
        self._emit_state()
        self.update()

    def _on_frame_ready(self, image):
        """收到离屏渲染帧：转成 QPixmap 并重绘（软件合成）。"""
        if image is None:
            self._model_pixmap = QPixmap()
            self._model_rect = QRect()
            self.update()
            return
        try:
            pm = QPixmap.fromImage(image)
            if pm.isNull():
                return
            self._model_pixmap = pm
            if self._runtime is not None:
                self._model_rect = self._runtime.frame_geometry()
            # 只重绘模型区域（避免整块舞台/渐变反复重绘）
            if self._model_rect.isValid():
                self.update(self._model_rect.adjusted(-2, -2, 2, 2))
            else:
                self.update()
        except Exception:
            pass

    def runtime_state(self) -> str:
        return self._runtime_state

    def runtime_message(self) -> str:
        return self._runtime_message

    def is_runtime_ready(self) -> bool:
        return self._runtime_state == "ready"

    def reload_model(self):
        self._model_pixmap = QPixmap()
        self._model_rect = QRect()
        if self._runtime is not None:
            try:
                self._runtime.shutdown(wait_ms=1500)
            except Exception:
                pass
            self._runtime = None
        self._runtime_state = "off"
        if self._runtime_wanted:
            self._start_runtime()

    def reset_view(self):
        if self._runtime is not None:
            try:
                self._runtime.reset_view()
            except Exception:
                pass

    # ── 视图几何 ──────────────────────────────────────────

    def _view_rect(self) -> QRect:
        w = max(1, self.width())
        h = max(1, self.height())
        mx = int(w * 0.10)
        top = int(h * 0.03)
        bottom = int(h * 0.88)
        return QRect(mx, top, w - mx * 2, max(40, bottom - top))

    def _sync_view(self):
        if self._runtime is None:
            return
        try:
            self._runtime.layout_view(self._view_rect())
        except Exception:
            pass

    # ── 事件 ──────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        if self._runtime_wanted and self._runtime_state == "off":
            self._start_runtime()
        else:
            self._sync_view()

    def hideEvent(self, event):
        super().hideEvent(event)
        # 离屏渲染无窗口合成问题，切页不需要特殊处理

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_view()

    def changeEvent(self, event):
        super().changeEvent(event)
        try:
            if event.type() == QEvent.WindowStateChange:
                if not self.window().isMinimized():
                    self._sync_view()
        except Exception:
            pass

    def paintEvent(self, event):
        """渐变背景（取自 SpaceBackground 缓存）+ 模型帧 + 状态文字。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        # 1) 背景：与主窗口渐变逐像素一致
        bg = getattr(self.window(), "space_bg", None)
        painted = False
        if bg is not None:
            try:
                painted = bg.paint_region(p, self)
            except Exception:
                painted = False
        if not painted:
            p.fillRect(self.rect(), QColor(22, 27, 52))

        # 2) 模型帧（软件合成，透明区保留背景）
        if (self._runtime_state == "ready"
                and not self._model_pixmap.isNull()):
            rect = self._model_rect
            if rect.isValid() and rect.width() > 0 and rect.height() > 0:
                p.drawPixmap(rect, self._model_pixmap)

        # 3) 可选静态立绘（仅在无 3D 且用户放置了素材时）
        elif (not self._char_pixmap.isNull()):
            avail_h = max(40.0, self.height() * 0.86)
            pm = self._char_pixmap
            scale = min(avail_h / pm.height(),
                        (self.width() * 0.5) / pm.width())
            tw, th = pm.width() * scale, pm.height() * scale
            p.drawPixmap(
                int((self.width() - tw) / 2),
                int(self.height() * 0.94 - th),
                int(tw), int(th), pm,
            )

        # 4) 状态文字
        self._paint_state_hint(p)
        p.end()

    def _paint_state_hint(self, p: QPainter):
        if self._runtime_state == "ready":
            return
        if self._runtime_state == "loading":
            text = self._runtime_message or "正在加载 Arona 模型…"
            color = QColor(190, 202, 240)
        elif self._runtime_state == "failed":
            first = (self._runtime_message or "模型加载失败").splitlines()
            text = "3D 模型不可用：" + (first[0] if first else "未知原因")
            color = QColor(255, 190, 205)
        elif self._runtime_state in ("disabled", "off"):
            text = "3D 运行时未启用（渐变背景）"
            color = QColor(160, 170, 205)
        else:
            return

        f = QFont()
        f.setPointSizeF(9.0)
        p.setFont(f)
        h = self.height()
        box = self.rect().adjusted(int(self.width() * 0.2), 0,
                                   -int(self.width() * 0.2), -int(h * 0.03))
        box.setTop(box.bottom() - 26)
        p.setPen(QPen(color))
        p.drawText(box, Qt.AlignCenter, text)

    # ── 鼠标交互（拖拽旋转 / 滚轮缩放）───────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._runtime is not None:
            self._drag_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
        self.clicked.emit()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_last is not None and self._runtime is not None:
            pos = event.position()
            dx = pos.x() - self._drag_last.x()
            dy = pos.y() - self._drag_last.y()
            self._drag_last = pos
            if abs(dx) > 0.5 or abs(dy) > 0.5:
                self._runtime.rotate_by(dx, dy)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_last = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._runtime is not None:
            delta = event.angleDelta().y()
            if delta:
                self._runtime.zoom_by(1.08 if delta > 0 else (1 / 1.08))
                event.accept()
                return
        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.fullscreen_requested.emit()
        super().mouseDoubleClickEvent(event)

    # ── 对外接口（保持兼容）─────────────────────────────────

    def set_status_text(self, text: str):
        self._status_text = text

    def get_status_text(self) -> str:
        return self._status_text

    def set_performance_mode(self, mode: str):
        if mode not in PERFORMANCE_MODES:
            return
        self._perf_mode = mode
        self._perf = PERFORMANCE_MODES[mode]
        if self._runtime is not None:
            try:
                self._runtime.set_performance_mode(mode)
            except Exception:
                pass
        self.update()

    def set_character_image(self, path: str) -> bool:
        try:
            pm = QPixmap(path)
            if pm.isNull():
                return False
            self._char_pixmap = pm
            self.update()
            return True
        except Exception:
            return False

    def show_stage(self, show: bool):
        self.setVisible(show)
        self.toggled.emit(show)
