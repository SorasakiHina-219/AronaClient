"""
spatial_ui.window_manager — 空间窗口管理器。

统一管理所有空间窗口的：
  - 创建与销毁
  - 位置与大小（含持久化）
  - Z-order 层级管理
  - 焦点管理
  - 动画过渡
  - 拖拽与缩放

设计原则：
  - 轻量级，不绑定具体业务
  - 支持性能降级（禁用动画/阴影）
  - 窗口几何信息持久化到 settings.json
"""

import warnings
warnings.filterwarnings("ignore")

import json
from pathlib import Path
from typing import Dict, Optional, Any

from PySide6.QtCore import (
    Qt,
    QPoint,
    QRect,
    QSize,
    QPropertyAnimation,
    QSequentialAnimationGroup,
    QEasingCurve,
    Signal,
    Slot,
    QTimer,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QGraphicsDropShadowEffect,
)

from spatial_ui.animations import (
    ANIM_WINDOW_SHOW_DURATION,
    ANIM_WINDOW_CLOSE_DURATION,
    EASING_OUT_CUBIC,
    EASING_IN_OUT_CUBIC,
)
from spatial_ui.styles import (
    SHADOW_BLUR,
    SHADOW_OFFSET,
    SHADOW_COLOR,
    PERFORMANCE_MODES,
)


class SpatialWindow:
    """
    空间窗口的轻量级描述符。

    不直接继承 QMainWindow，而是包装一个已有窗口，
    为其添加空间交互能力（动画、层级、持久化）。
    """

    _next_z = 100  # 初始 Z 起始值

    def __init__(
        self,
        widget: QWidget,
        *,
        window_id: str = "",
        persist_key: str = "",
        default_geometry: Optional[QRect] = None,
        animation_enabled: bool = True,
        performance_mode: str = "medium",
        settings_path: Optional[Path] = None,
    ):
        self.widget = widget
        self.window_id = window_id or f"sw_{id(widget)}"
        self.persist_key = persist_key or self.window_id
        # 几何持久化路径（None = 默认 AppData settings.json；
        # 预览模式传入独立目录，避免写入正式用户数据）
        self._settings_path = settings_path

        # ── 层级管理 ──────────────────────────────
        self.z_order = SpatialWindow._next_z
        SpatialWindow._next_z += 1
        self._focus_stack: list = []

        # ── 动画控制 ──────────────────────────────
        self._anim_enabled = animation_enabled
        self._perf_mode = performance_mode
        self._perf_config = PERFORMANCE_MODES.get(
            performance_mode, PERFORMANCE_MODES["medium"]
        )

        # ── 阴影效果 ──────────────────────────────
        self._shadow_effect: Optional[QGraphicsDropShadowEffect] = None
        if self._perf_config.get("shadow", True):
            self._shadow_effect = QGraphicsDropShadowEffect(widget)
            self._shadow_effect.setBlurRadius(SHADOW_BLUR)
            self._shadow_effect.setOffset(0, SHADOW_OFFSET)
            self._shadow_effect.setColor(
                QApplication.desktop().screenGeometry(0).isNull()
                and __import__("PySide6.QtGui").QtGui.QColor(SHADOW_COLOR)
                or __import__("PySide6.QtGui").QtGui.QColor(SHADOW_COLOR)
            )
            widget.setGraphicsEffect(self._shadow_effect)

        # ── 初始几何 ──────────────────────────────
        self._default_geometry = default_geometry
        self._loaded_geometry: Optional[QRect] = None

    # ── 显示 / 隐藏 ────────────────────────────────────────────

    def show(self, animated: bool = True):
        """显示窗口，带淡入动画（如启用）。"""
        if self._loaded_geometry:
            self.widget.setGeometry(self._loaded_geometry)
        elif self._default_geometry:
            self.widget.setGeometry(self._default_geometry)

        self.widget.show()
        self.widget.raise_()
        self._bring_to_front()

        if animated and self._anim_enabled and self._perf_config.get("anim_speed", 1.0) > 0:
            self._animate_show()
        else:
            self.widget.setWindowOpacity(1.0)

    def hide(self, animated: bool = True):
        """隐藏窗口，带淡出动画（如启用）。"""
        if animated and self._anim_enabled and self._perf_config.get("anim_speed", 1.0) > 0:
            self._animate_close()
        else:
            self.widget.hide()
            self.widget.setWindowOpacity(1.0)

    def close(self):
        """关闭并清理。"""
        self.widget.close()
        if self._shadow_effect:
            self._shadow_effect.deleteLater()
            self._shadow_effect = None

    # ── 动画 ───────────────────────────────────────────────────

    def _animate_show(self):
        dur = int(ANIM_WINDOW_SHOW_DURATION * (1.0 / max(self._perf_config.get("anim_speed", 1.0), 0.1)))
        anim = QPropertyAnimation(self.widget, b"windowOpacity")
        anim.setDuration(dur)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type(EASING_OUT_CUBIC))
        anim.start()

    def _animate_close(self):
        dur = int(ANIM_WINDOW_CLOSE_DURATION * (1.0 / max(self._perf_config.get("anim_speed", 1.0), 0.1)))
        anim = QPropertyAnimation(self.widget, b"windowOpacity")
        anim.setDuration(dur)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type(EASING_IN_OUT_CUBIC))
        anim.finished.connect(self.widget.hide)
        anim.start()

    # ── 层级与焦点 ─────────────────────────────────────────────

    def _bring_to_front(self):
        """将该窗口提升到 Z 序顶层。"""
        self.z_order = SpatialWindow._next_z
        SpatialWindow._next_z += 1
        self.widget.raise_()
        self._focus_stack.append(self.window_id)

    def focus(self):
        self._bring_to_front()
        self.widget.activateWindow()

    def is_focused(self) -> bool:
        return (
            self._focus_stack
            and self._focus_stack[-1] == self.window_id
        )

    # ── 几何持久化 ─────────────────────────────────────────────

    def save_geometry(self):
        """保存当前窗口几何到内存缓存。"""
        geo = self.widget.geometry()
        self._loaded_geometry = QRect(
            geo.x(), geo.y(), geo.width(), geo.height()
        )

    def load_geometry(self, default: Optional[QRect] = None) -> Optional[QRect]:
        """
        加载保存的几何，或返回默认值。
        优先从 settings.json 读取。
        """
        if self._loaded_geometry:
            return self._loaded_geometry

        saved = self._load_from_settings()
        if saved:
            self._loaded_geometry = QRect(
                saved["x"], saved["y"],
                saved["width"], saved["height"],
            )
            return self._loaded_geometry

        return default or self._default_geometry

    def _default_settings_path(self) -> Path:
        """默认几何持久化路径（AppData）。"""
        return Path.home() / "AppData" / "Roaming" / "AronaClient" / "settings.json"

    def _settings_file(self) -> Path:
        return (
            self._settings_path
            if self._settings_path is not None
            else self._default_settings_path()
        )

    def _load_from_settings(self) -> Optional[Dict]:
        """从 settings.json 加载窗口几何。"""
        try:
            settings_path = self._settings_file()
            if not settings_path.exists():
                return None
            with settings_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            windows = data.get("spatial_windows", {})
            return windows.get(self.persist_key)
        except Exception:
            return None

    def save_to_settings(self):
        """将当前几何写入 settings.json。"""
        geo = self.widget.geometry()
        geom_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height(),
        }
        try:
            settings_path = self._settings_file()
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if settings_path.exists():
                with settings_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            data.setdefault("spatial_windows", {})
            data["spatial_windows"][self.persist_key] = geom_data
            with settings_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 静默失败，不影响主功能


class SpatialWindowManager:
    """
    空间窗口管理器。

    统一管理应用中所有 SpatialWindow 实例。
    提供注册、查找、层级管理、批量操作等能力。
    """

    def __init__(
        self,
        performance_mode: str = "medium",
        settings_path: Optional[Path] = None,
    ):
        self._windows: Dict[str, SpatialWindow] = {}
        self._performance_mode = performance_mode
        self._perf_config = PERFORMANCE_MODES.get(
            performance_mode, PERFORMANCE_MODES["medium"]
        )
        # 几何持久化路径（预览模式传入独立文件，默认 None = AppData）
        self._settings_path = settings_path

    @property
    def performance_mode(self) -> str:
        return self._performance_mode

    @property
    def performance_config(self) -> dict:
        return self._perf_config

    def register(
        self,
        widget: QWidget,
        *,
        window_id: str = "",
        persist_key: str = "",
        default_geometry: Optional[QRect] = None,
    ) -> SpatialWindow:
        """注册一个窗口到管理器。"""
        sw = SpatialWindow(
            widget,
            window_id=window_id,
            persist_key=persist_key,
            default_geometry=default_geometry,
            performance_mode=self._performance_mode,
            settings_path=self._settings_path,
        )
        self._windows[sw.window_id] = sw
        return sw

    def get(self, window_id: str) -> Optional[SpatialWindow]:
        """按 ID 查找窗口。"""
        return self._windows.get(window_id)

    def show_window(
        self,
        window_id: str,
        animated: bool = True,
    ) -> bool:
        """显示指定窗口。"""
        sw = self._windows.get(window_id)
        if sw is None:
            return False
        sw.show(animated=animated)
        return True

    def hide_window(
        self,
        window_id: str,
        animated: bool = True,
    ) -> bool:
        """隐藏指定窗口。"""
        sw = self._windows.get(window_id)
        if sw is None:
            return False
        sw.hide(animated=animated)
        return True

    def close_window(self, window_id: str) -> bool:
        """关闭并注销窗口。"""
        sw = self._windows.pop(window_id, None)
        if sw is None:
            return False
        sw.close()
        return True

    def focus_window(self, window_id: str) -> bool:
        """将窗口置于顶层并激活。"""
        sw = self._windows.get(window_id)
        if sw is None:
            return False
        sw.focus()
        return True

    def save_all_geometry(self):
        """保存所有已注册窗口的几何。"""
        for sw in self._windows.values():
            sw.save_geometry()
            sw.save_to_settings()

    def list_windows(self) -> list:
        """返回所有已注册窗口的 ID 列表。"""
        return list(self._windows.keys())

    def set_performance_mode(self, mode: str):
        """运行时切换性能模式。"""
        if mode not in PERFORMANCE_MODES:
            return
        self._performance_mode = mode
        self._perf_config = PERFORMANCE_MODES[mode]
        for sw in self._windows.values():
            sw._perf_mode = mode
            sw._perf_config = self._perf_config
            # 更新阴影
            if sw._shadow_effect:
                sw._shadow_effect.setEnabled(
                    self._perf_config.get("shadow", True)
                )
