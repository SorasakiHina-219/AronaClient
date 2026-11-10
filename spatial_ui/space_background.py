"""
spatial_ui.space_background — 空间环境背景（缓存绘制）。

绘制一层夜间蓝紫空间环境：
  - 垂直渐变夜空
  - 顶部冷色月光光晕
  - 静态散景光点（bokeh，仅 high 模式）
  - 底部暗角

性能：全部绘制结果缓存为 QPixmap，仅在 resize / 换模式时重绘一次；
      运行时 window 重绘只贴图，低端机无压力。
"""

import warnings
warnings.filterwarnings("ignore")

import random
from typing import List, Tuple

from PySide6.QtCore import Qt, QRectF, QRect, QPoint, QPointF
from PySide6.QtGui import (
    QPainter,
    QPixmap,
    QColor,
    QBrush,
    QPainterPath,
    QPen,
    QRadialGradient,
    QLinearGradient,
)
from PySide6.QtWidgets import QWidget

from spatial_ui.styles import (
    COLOR_SPACE_TOP,
    COLOR_SPACE_CENTER,
    COLOR_SPACE_EDGE,
    WINDOW_RADIUS,
    PERFORMANCE_MODES,
)


class SpaceBackground(QWidget):
    """空间环境背景层。"""

    def __init__(
        self,
        parent=None,
        *,
        performance_mode: str = "medium",
        enable_bokeh: bool = True,
    ):
        super().__init__(parent)

        self._perf_mode = performance_mode
        self._perf = PERFORMANCE_MODES.get(
            performance_mode, PERFORMANCE_MODES["medium"]
        )
        self._enable_bokeh = (
            enable_bokeh and self._perf.get("particles", False)
        )

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        self._cache: QPixmap = QPixmap()
        self._bokeh: List[Tuple[float, float, float, Tuple[int, int, int], int]] = []
        self._seed_bokeh()

    # ── 数据 ──────────────────────────────────────────────

    def _seed_bokeh(self):
        """固定随机种子，保证每次启动光点位置稳定。"""
        rng = random.Random(20261105)
        self._bokeh = []
        palette = [
            (140, 165, 255),
            (240, 166, 200),
            (167, 139, 250),
            (120, 200, 255),
        ]
        for _ in range(26):
            self._bokeh.append(
                (
                    rng.uniform(0.02, 0.98),
                    rng.uniform(0.05, 0.85),
                    rng.uniform(6.0, 26.0),
                    palette[rng.randrange(len(palette))],
                    rng.randint(10, 30),
                )
            )

    # ── 缓存 ──────────────────────────────────────────────

    def _rebuild_cache(self):
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(0, 0, w, h)

        # 0) 外层圆角裁剪（空间窗口感）
        radius = float(WINDOW_RADIUS)
        outer = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        clip = QPainterPath()
        clip.addRoundedRect(outer, radius, radius)
        painter.setClipPath(clip)

        # 1) 基础垂直渐变
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, QColor(COLOR_SPACE_TOP))
        grad.setColorAt(0.45, QColor(COLOR_SPACE_CENTER))
        grad.setColorAt(1.0, QColor(COLOR_SPACE_EDGE))
        painter.fillRect(rect, QBrush(grad))

        # 2) 顶部冷月光晕
        glow = QRadialGradient(
            QPointF(w * 0.52, -h * 0.15),
            max(w, h) * 0.75,
        )
        glow.setColorAt(0.0, QColor(150, 175, 255, 46))
        glow.setColorAt(0.55, QColor(110, 130, 230, 16))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, QBrush(glow))

        # 3) 左下樱色环境光
        glow2 = QRadialGradient(
            QPointF(w * 0.08, h * 1.02),
            max(w, h) * 0.55,
        )
        glow2.setColorAt(0.0, QColor(240, 166, 200, 30))
        glow2.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, QBrush(glow2))

        # 4) 散景光点（仅 high 模式）
        if self._enable_bokeh:
            for bx, by, br, (cr, cg, cb), alpha in self._bokeh:
                g = QRadialGradient(QPointF(bx * w, by * h), br)
                g.setColorAt(0.0, QColor(cr, cg, cb, alpha))
                g.setColorAt(0.6, QColor(cr, cg, cb, max(0, alpha // 3)))
                g.setColorAt(1.0, QColor(cr, cg, cb, 0))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(g))
                painter.drawEllipse(QPointF(bx * w, by * h), br, br)

        # 5) 底部暗角，突出内容
        vig = QLinearGradient(QPointF(0, h * 0.62), QPointF(0, h))
        vig.setColorAt(0.0, QColor(4, 7, 18, 0))
        vig.setColorAt(1.0, QColor(4, 7, 18, 150))
        painter.fillRect(QRectF(0, h * 0.62, w, h * 0.38), QBrush(vig))

        # 6) 外层窗口描边（空间窗口轮廓）
        painter.setClipping(False)
        painter.setPen(QPen(QColor(160, 182, 255, 70), 1.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(outer, radius, radius)

        painter.end()
        self._cache = pm

    # ── 事件 ──────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rebuild_cache()

    def showEvent(self, event):
        super().showEvent(event)
        if self._cache.isNull() or self._cache.size() != self.size():
            self._rebuild_cache()

    def paintEvent(self, event):
        if self._cache.isNull() or self._cache.size() != self.size():
            self._rebuild_cache()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)
        painter.end()

    # ── 供子层复用同一份背景（避免 GL 合成时底下是脏的 backing store）──

    def cache(self) -> QPixmap:
        if self._cache.isNull() or self._cache.size() != self.size():
            self._rebuild_cache()
        return self._cache

    def blur_patch(self, target_widget, radius: int = 6) -> QPixmap:
        """返回目标控件背后的**背景层**模糊贴片。

        隔离性：输入只有 SpaceBackground 自己的渐变缓存（背景层），
        绝不包含聊天 UI / 主窗口截图，符合"背景模糊必须使用隔离渲染输入"。

        实现：降采样 → 平滑放大（低成本 box blur 近似），低端机可用。
        """
        try:
            cache = self.cache()
            if cache.isNull():
                return QPixmap()
            tl_global = target_widget.mapToGlobal(QPoint(0, 0))
            tl = self.mapFromGlobal(tl_global)
            src = QRect(tl.x(), tl.y(),
                        target_widget.width(), target_widget.height())
            src = src.intersected(QRect(0, 0, cache.width(), cache.height()))
            if src.width() <= 0 or src.height() <= 0:
                return QPixmap()
            patch = cache.copy(src)
            if radius > 0:
                div = max(2, int(radius))
                small = patch.scaled(
                    max(1, patch.width() // div),
                    max(1, patch.height() // div),
                    Qt.IgnoreAspectRatio,
                    Qt.SmoothTransformation,
                )
                patch = small.scaled(
                    patch.size(),
                    Qt.IgnoreAspectRatio,
                    Qt.SmoothTransformation,
                )
            return patch
        except Exception:
            return QPixmap()

    def paint_region(self, painter, target_widget) -> bool:
        """把背景缓存中与 target_widget 对应的区域画到它自己的区域内。

        这样模型舞台（GL 透明叠加的宿主）自己就有一份正确的渐变背景，
        GL 合成时不会露出未重绘的深色 backing store。
        """
        try:
            cache = self.cache()
            if cache.isNull():
                return False
            # space_bg 与目标控件是兄弟关系，不能用 mapTo（会提示不在父子链上）
            tl_global = target_widget.mapToGlobal(QPoint(0, 0))
            tl = self.mapFromGlobal(tl_global)
            src = QRect(tl.x(), tl.y(),
                        target_widget.width(), target_widget.height())
            src = src.intersected(QRect(0, 0, cache.width(), cache.height()))
            if src.width() <= 0 or src.height() <= 0:
                return False
            painter.drawPixmap(
                QRect(0, 0, target_widget.width(), target_widget.height()),
                cache,
                src,
            )
            return True
        except Exception:
            return False

    def set_performance_mode(self, mode: str):
        if mode not in PERFORMANCE_MODES:
            return
        self._perf_mode = mode
        self._perf = PERFORMANCE_MODES[mode]
        self._enable_bokeh = self._perf.get("particles", False)
        self._rebuild_cache()
        self.update()

    def get_performance_mode(self) -> str:
        return self._perf_mode
