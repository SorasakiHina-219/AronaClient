"""
spatial_ui.model_runtime — Arona 3D 模型运行时（离屏渲染 + 软件合成）。

为什么改成离屏渲染？
  实测（PySide6 6.12 + Intel HD 4400 + Windows 10 + 透明顶层窗口）：
  QOpenGLWidget 的 FBO 中 alpha=0 的区域在 Qt 合成到窗口时被当作**不透明黑**，
  即使显式设置 QSurfaceFormat(alpha=8) 也一样。
  结果就是模型背后出现一块黑色矩形，永远看不到渐变背景——这是「背景串层」的根因。

  因此本模块改为：
    QOpenGLWidget(WA_DontShowOnScreen) 离屏渲染
        → grabFramebuffer() 得到带 alpha 的 QImage
        → ModelStage 用 QPainter 把它叠加在渐变背景上（软件合成，alpha 正确）

架构（与聊天/窗口管理/AIClient 完全解耦）：
    ModelStage
      └── AronaModelRuntime
            ├── PmxLoadWorker（mascot_3d，后台线程：解析/蒙皮/贴图）
            └── MascotGLView（mascot_3d，离屏 QOpenGLWidget，仅渲染不显示）
"""

import sys
import warnings
warnings.filterwarnings("ignore")

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QThread,
    QRect,
    QTimer,
    Qt,
    Signal,
    Slot,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# M0-A：模型路径统一由 runtime.model3d.asset_provider 解析（ADR-004 §3.2/§3.3/§9.2）。
# 本模块不再自行拼接模型路径；默认常量仅作展示/兼容占位，真实加载必须走 Provider。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.model3d.asset_provider import ModelAssetProvider  # noqa: E402

_MODEL_PROVIDER = ModelAssetProvider(repo_asset_root=PROJECT_ROOT / "assets" / "3d")


def resolve_model_asset(model_id=None):
    """解析默认模型资源；返回 AssetResolution（永不抛异常）。"""
    return _MODEL_PROVIDER.resolve(model_id)


# 兼容旧引用（未校验占位值，仅用于显示/日志）
DEFAULT_PMX = PROJECT_ROOT / "assets" / "3d" / "arona" / "Arona_1.01" / "Arona_1.01.pmx"
DEFAULT_MODEL_DIR = DEFAULT_PMX.parent

TEXTURE_MAX_BY_MODE = {
    "high": 2048,
    "medium": 1024,
    "low": 512,
}

# 离屏渲染分辨率比例（grabFramebuffer 是主要开销，低档降采样换帧率）
RENDER_SCALE_BY_MODE = {
    "high": 1.0,
    "medium": 0.85,
    "low": 0.68,
}

# 交互重绘最小间隔（ms）：约 30 fps
RENDER_INTERVAL_MS = 33


class AronaModelRuntime(QObject):
    """Arona 3D 模型运行时（离屏渲染 + 帧回调）。"""

    progress = Signal(str)
    ready = Signal()
    failed = Signal(str)
    state_changed = Signal(str)
    frame_ready = Signal(object)     # QImage（带 alpha），None 表示清空

    def __init__(
        self,
        host_widget,
        *,
        model_path: Optional[Path] = None,
        model_dir: Optional[Path] = None,
        performance_mode: str = "medium",
        parent=None,
    ):
        super().__init__(parent or host_widget)

        self._host = host_widget
        self._model_path = Path(model_path) if model_path else DEFAULT_PMX
        self._model_dir = Path(model_dir) if model_dir else self._model_path.parent
        self._perf_mode = performance_mode

        self._thread: Optional[QThread] = None
        self._worker = None
        self._view = None
        self._state = "idle"
        self._zoom = 1.0

        self._resolution = None
        self._layout_rect = QRect(0, 0, 1, 1)
        self._frame_geometry = QRect()
        self._render_timer: Optional[QTimer] = None
        self._load_t0 = 0.0

    # ── 状态 ──────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str):
        self._state = state
        self.state_changed.emit(state)

    def view(self):
        """离屏 GL 部件（仅供诊断/清理，不再作为可见子控件）。"""
        return self._view

    @property
    def model_path(self) -> Path:
        return self._model_path

    def model_available(self) -> bool:
        """M0-A：可用性 = Provider 解析 + SHA256 校验通过（结果缓存）。"""
        res = self._resolution or resolve_model_asset()
        self._resolution = res
        return bool(res.ok)

    def frame_geometry(self) -> QRect:
        """当前帧在宿主坐标系中的目标矩形。"""
        return QRect(self._frame_geometry)

    # ── 启动 / 停止 ───────────────────────────────────────

    def start(self):
        if self._state in ("loading", "ready"):
            return
        # M0-A：统一走 Provider 解析 + SHA256 校验；失败则降级 2D（不发 3D 帧）
        resolution = self._resolution
        if resolution is None:
            resolution = resolve_model_asset()
            self._resolution = resolution
        if not resolution.ok or resolution.path is None:
            self._set_state("failed")
            self.failed.emit(
                "3D 模型不可用，已降级 2D：" + resolution.message
            )
            return
        self._model_path = resolution.path
        self._model_dir = resolution.path.parent

        try:
            from mascot_3d import PmxLoadWorker
        except Exception as exc:
            self._set_state("failed")
            self.failed.emit(f"3D 模块不可用：{type(exc).__name__}: {exc}")
            return

        self._set_state("loading")
        self._load_t0 = time.perf_counter()

        tex_max = TEXTURE_MAX_BY_MODE.get(self._perf_mode, 1024)

        self._thread = QThread()
        self._worker = PmxLoadWorker(
            self._model_path,
            self._model_dir,
            pose=None,
            height=320,
            texture_max=tex_max,
            resolution=resolution,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.loaded.connect(self._on_loaded)
        self._worker.failed.connect(self._on_failed)
        self._worker.loaded.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    @Slot(object)
    def _on_loaded(self, payload):
        if self._state != "loading":
            return
        try:
            from mascot_3d import get_gl_view_class

            GLView = get_gl_view_class()
            view = GLView(self._host)
            # 关键：离屏渲染，绝不参与窗口合成
            view.setAttribute(Qt.WA_DontShowOnScreen, True)
            view.setAttribute(Qt.WA_TranslucentBackground, True)
            view.setAttribute(Qt.WA_NoSystemBackground, True)
            view.set_payload(payload)
            view.setMinimumSize(1, 1)
            view.setMaximumSize(1 << 16, 1 << 16)
            # 移出宿主可视区域 + 不接收鼠标：它只做离屏渲染，绝不参与合成/交互
            view.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            view.move(-20000, -20000)

            self._view = view
            self._apply_geometry()
            try:
                view.show()          # 触发 initializeGL（离屏，不显示到屏幕）
            except Exception:
                pass

            # 等 GL 初始化完成后再渲染首帧
            QTimer.singleShot(120, self._render_now)
            QTimer.singleShot(400, self._render_now)

            self._set_state("ready")
            elapsed = time.perf_counter() - self._load_t0
            self.progress.emit(f"模型已加载（{elapsed:.1f}s）")
            self.ready.emit()

            # 回收加载线程与 worker（payload 约 19 MB）
            try:
                if self._thread is not None:
                    self._thread.quit()
                    self._thread.wait(3000)
            except Exception:
                pass
            try:
                if self._worker is not None:
                    self._worker.deleteLater()
            except Exception:
                pass
            self._worker = None
            self._thread = None
        except Exception as exc:
            self._set_state("failed")
            self.failed.emit(
                f"创建 3D 渲染器失败：{type(exc).__name__}: {exc}"
            )

    @Slot(str)
    def _on_failed(self, message):
        if self._state not in ("loading", "ready"):
            return
        self._set_state("failed")
        self.failed.emit(str(message))

    @Slot()
    def _on_thread_finished(self):
        self._worker = None
        self._thread = None

    def shutdown(self, wait_ms: int = 4000):
        """取消加载并释放 GL 资源。可重复调用。"""
        try:
            if self._worker is not None:
                self._worker.cancel()
        except Exception:
            pass
        try:
            if self._thread is not None and self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(wait_ms)
        except Exception:
            pass
        self._destroy_view()
        if self._state != "disabled":
            self._set_state("idle")

    def _destroy_view(self):
        view = self._view
        self._view = None
        self._frame_geometry = QRect()
        if view is None:
            return
        try:
            view.cleanup()
        except Exception:
            pass
        try:
            view.hide()
            view.deleteLater()
        except Exception:
            pass

    def reload(self):
        self.shutdown(wait_ms=1500)
        self.start()

    def disable(self):
        self.shutdown(wait_ms=1500)
        try:
            self.frame_ready.emit(None)
        except Exception:
            pass
        self._set_state("disabled")

    # ── 性能模式 ──────────────────────────────────────────

    def set_performance_mode(self, mode: str):
        self._perf_mode = mode

    # ── 布局 / 渲染 ───────────────────────────────────────

    def layout_view(self, available: QRect):
        """宿主 resize 时调用：重新计算模型在宿主中的目标矩形并重绘。"""
        self._layout_rect = QRect(available)
        self._apply_geometry()
        self._schedule_render()

    def _apply_geometry(self):
        r = self._layout_rect
        if r.width() <= 4 or r.height() <= 4:
            return
        target_h = int(r.height() * 0.94 * self._zoom)
        target_w = int(min(r.width() * 0.92, target_h * 0.62))
        target_h = int(min(target_h, r.height() * 1.6))
        x = r.x() + (r.width() - target_w) // 2
        y = r.y() + r.height() - target_h
        self._frame_geometry = QRect(x, y, max(24, target_w), max(24, target_h))

        view = self._view
        if view is not None:
            try:
                scale = RENDER_SCALE_BY_MODE.get(self._perf_mode, 1.0)
                view.resize(max(24, int(self._frame_geometry.width() * scale)),
                            max(24, int(self._frame_geometry.height() * scale)))
                view.move(-20000, -20000)   # 保持离屏
            except Exception:
                pass

    def _schedule_render(self):
        """合并式重绘请求（≤30fps），避免拖动时堆积。"""
        if self._view is None or self._state != "ready":
            return
        if self._render_timer is None:
            self._render_timer = QTimer(self)
            self._render_timer.setSingleShot(True)
            self._render_timer.setInterval(RENDER_INTERVAL_MS)
            self._render_timer.timeout.connect(self._render_now)
        if not self._render_timer.isActive():
            self._render_timer.start()

    @Slot()
    def _render_now(self):
        view = self._view
        if view is None or self._state != "ready":
            return
        try:
            img = view.grabFramebuffer()
            self.frame_ready.emit(img)
        except Exception:
            pass

    # ── 视角控制 ─────────────────────────────────────────

    def set_zoom(self, zoom: float):
        self._zoom = max(0.55, min(1.9, float(zoom)))
        self._apply_geometry()
        self._schedule_render()

    def zoom_by(self, factor: float):
        self.set_zoom(self._zoom * factor)

    def reset_view(self):
        view = self._view
        if view is not None:
            try:
                view.yaw = 180.0
                view.pitch = 0.0
            except Exception:
                pass
        self.set_zoom(1.0)

    def rotate_by(self, dx: float, dy: float):
        view = self._view
        if view is None or self._state != "ready":
            return
        try:
            view.yaw = (float(getattr(view, "yaw", 180.0)) + dx * 0.5) % 360.0
            view.pitch = max(
                -35.0, min(35.0, float(getattr(view, "pitch", 0.0)) + dy * 0.4)
            )
            self._schedule_render()
        except Exception:
            pass

    # ── 展示控制（兼容旧接口）─────────────────────────────

    def show_view(self):
        self._schedule_render()

    def hide_view(self):
        pass

    def _kick_composite(self):
        """兼容旧接口：现在由 frame_ready 驱动合成。"""
        self._schedule_render()
