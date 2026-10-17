# -*- coding: utf-8 -*-
"""
core/desktop_awareness.py
AronaClient 桌面自主感知 V1（含 V1.1 修复）

职责：
  - 在后台线程按固定间隔采集桌面截图
  - 检测有意义的画面变化（基于感知哈希 pHash）
  - 变化时调用 AI 分析，生成简短提醒
  - 去重：同一持续事件不重复提醒
  - 支持 start / pause / resume / stop

安全约束：
  - 通过 DesktopVision.capture() 截图（复用 desktop_access 门禁）
  - 每次 AI 分析前重新检查 DV 授权
  - 不与 computer_control 共享开关
  - 不调用真实鼠标/键盘操作
  - 截图仅临时缓存，不落盘持久化
  - 事件通过 Qt 信号路由到主线程回调

线程安全：
  - _state / _worker / _thread 受 self._lock 保护
  - worker.run() 永不持锁
  - _on_event_from_worker() 通过 worker_token 隔离旧线程事件
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, QObject, Qt, Signal
from PySide6.QtGui import QImage

from core.desktop_vision import ScreenShot

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

# 默认采集间隔（秒）
DEFAULT_INTERVAL_SECONDS = 30

# 最小采集间隔（秒）—— 防止过快采样导致资源耗尽
MIN_INTERVAL_SECONDS = 5

# 最大连续未变化轮次后才重新提醒同一事件（去重窗口）
DEDUP_WINDOW_ROUNDS = 3

# 保持的历史事件数量上限（避免内存无限增长）
MAX_EVENT_HISTORY = 20

# AI 分析超时（秒）
ANALYSIS_TIMEOUT_SECONDS = 60


# ============================================================
# 数据类型
# ============================================================

class AwarenessState(Enum):
    """感知引擎状态机。"""
    STOPPED = "stopped"       # 已停止（初始状态）
    RUNNING = "running"       # 正在运行（采集 + 分析中）
    PAUSED = "paused"         # 已暂停（定时器停止，但引擎未销毁）

    @property
    def is_running(self) -> bool:
        return self == AwarenessState.RUNNING

    @property
    def is_stopped(self) -> bool:
        return self == AwarenessState.STOPPED

    @property
    def is_paused(self) -> bool:
        return self == AwarenessState.PAUSED


@dataclass
class WorkerToken:
    """每次 start/pause 恢复产生的唯一身份标记。

    用于在 _on_event_from_worker 中过滤来自旧 worker 的延迟事件。
    """
    generation: int
    worker_id: int


# 全局 counter，用于为每次启动分配唯一 worker ID
_worker_id_counter = 0


def _next_worker_id() -> int:
    global _worker_id_counter
    _worker_id_counter += 1
    return _worker_id_counter


@dataclass
class DesktopAwarenessEvent:
    """一次桌面感知事件。"""
    timestamp: float               # 时间戳（time.monotonic）
    description: str               # 简短描述
    detail: str = ""               # 详细分析结果（由 AI 提供）
    is_important: bool = True      # 是否为重要事件
    dedup_key: str = ""            # 去重键（pHash 摘要）
    status: str = "new"            # new | sent | suppressed
    error: Optional[str] = None    # 错误信息（如有）
    worker_token: Optional[WorkerToken] = None  # 来源 worker 标识

    def summary(self) -> str:
        if self.error:
            return f"感知失败：{self.error}"
        return self.description or self.detail or "(无内容)"


# ============================================================
# 感知哈希工具函数
# ============================================================

def _compute_ahash(shot: ScreenShot) -> Optional[str]:
    """
    对 ScreenShot 计算 32x32 平均感知哈希（aHash）。

    步骤：
      1. 从 shot.image（QImage）或 base64 fallback 获取图像
      2. 缩放到 32x32 灰度图（忽略宽高比，填满整个画布）
      3. 计算每个像素的亮度（Gray8 格式直接取像素值）
      4. 与全局均值比较，>= 均值记 1，否则记 0
      5. 返回 1024 位二进制字符串

    相同画面 → 完全一致的 hash；不同画面 → 汉明距离显著增大。
    此算法对轻微 JPEG 压缩噪声不敏感，适合桌面变化检测。
    """
    try:
        # 尝试直接获取原始 QImage（capture() 保存在 shot.image）
        raw_image = shot.image
        if raw_image is None:
            # fallback：从 base64 解码 JPEG 重建 QImage
            raw_image = _image_from_data_url(shot.data_url)
        if raw_image is None or raw_image.isNull():
            return None

        # 缩放到 32x32 灰度图（填满整个画布，不做宽高比适配）
        small = raw_image.scaled(
            32, 32,
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )
        small = small.convertToFormat(QImage.Format_Grayscale8)

        if small.isNull():
            return None

        # 收集所有像素亮度值（Grayscale8: pixelColor().red() == gray value）
        lum_values = []
        for y in range(32):
            for x in range(32):
                lum = small.pixelColor(x, y).red()  # Gray8: R=G=B=gray
                lum_values.append(lum)

        if not lum_values:
            return None

        mean_lum = sum(lum_values) / len(lum_values)

        # 每个像素与均值比较，生成 1024-bit 二进制字符串
        bits = ['1' if lum >= mean_lum else '0' for lum in lum_values]
        return ''.join(bits)

    except Exception as exc:
        logger.debug("桌面感知：pHash 计算失败：%s", exc)
        return None


def _image_from_data_url(data_url: str) -> Optional[QImage]:
    """从 data:image/jpeg;base64,... 格式字符串重建 QImage。"""
    try:
        if not data_url.startswith("data:image/jpeg;base64,"):
            return None
        b64 = data_url[len("data:image/jpeg;base64,"):]
        raw = base64.b64decode(b64)
        img = QImage()
        img.loadFromData(raw, "JPEG")
        return img if not img.isNull() else None
    except Exception:
        return None


def _hamming_distance(a: str, b: str) -> int:
    """计算两个等长二进制字符串的汉明距离（不同 bit 的数量）。"""
    if not a or not b or len(a) != len(b):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(a, b))


# ============================================================
# 后台工作线程（QObject + QThread）
# ============================================================

class _AwarenessWorker(QObject):
    """
    后台线程工作对象。

    运行循环：
      1. 调用 DesktopVision.capture() 获取截图
      2. 计算感知哈希（pHash），与上次比较
      3. 如有变化，调用 vision.analyze() 获取 AI 分析结果
      4. 通过 event_ready 信号通知主线程

    注意：
      - 本类所有属性访问都在 worker 线程内执行
      - 回调（event_ready.emit）在主线程执行（Qt 自动排队）
      - 不持有对主线程 QObject 的引用，避免跨线程问题
    """

    # 信号：主线程安全的事件通知（Qt 自动排队到主线程）
    event_ready = Signal(object)

    def __init__(
        self,
        ai_client: Any,
        interval_seconds: float,
        token: WorkerToken,
        dedup_window: int = DEDUP_WINDOW_ROUNDS,
        max_history: int = MAX_EVENT_HISTORY,
    ):
        super().__init__()
        self._ai_client = ai_client
        self._interval = max(interval_seconds, MIN_INTERVAL_SECONDS)
        self._token = token
        self._dedup_window = dedup_window
        self._max_history = max(max_history, 5)

        # 内部状态
        self._running = False
        self._prev_phash: Optional[str] = None
        self._event_history: list[DesktopAwarenessEvent] = []
        self._change_count_since_last_alert = 0

    # ── 公共接口（必须在 worker 线程内调用）───────────────────

    def start_loop(self) -> None:
        """开始感知循环。"""
        self._running = True
        self._prev_phash = None
        self._change_count_since_last_alert = 0
        logger.info(
            "桌面感知：循环启动 [gen=%d, id=%d]，间隔 %.1f 秒",
            self._token.generation, self._token.worker_id,
            self._interval,
        )

    def stop_loop(self) -> None:
        """停止感知循环。"""
        self._running = False
        logger.info(
            "桌面感知：循环停止 [gen=%d, id=%d]",
            self._token.generation, self._token.worker_id,
        )

    # ── 核心循环（由 QThread 调用 run() 触发）─────────────────

    def run(self) -> None:
        """
        后台感知主循环。
        每轮：截图 → pHash 比较 → 如变化则 AI 分析 → 回调通知。
        """
        self.start_loop()

        while self._running:
            try:
                self._cycle()
            except Exception as exc:
                logger.warning("桌面感知单轮异常：%s", exc)

            # 非阻塞等待：每 _interval 秒一轮
            self._sleep_interval()

        self.stop_loop()

    # ── 内部方法 ─────────────────────────────────────────────

    def _sleep_interval(self) -> None:
        """线程安全的间歇等待。"""
        end = time.monotonic() + self._interval
        while self._running and time.monotonic() < end:
            # 每 0.1 秒检查一次停止标志，响应及时
            time.sleep(min(0.1, end - time.monotonic()))

    def _cycle(self) -> None:
        """执行一轮：截图 → pHash 比较 → 分析（如有变化）。"""
        # 1. 获取截图（复用已有授权门禁，仅 DV 开关即可）
        shot = self._capture_screenshot()
        if not shot:
            return  # 授权拒绝或截图失败，跳过本轮

        # 2. 计算感知哈希
        phash = self._compute_ahash(shot)
        if phash is None:
            return

        # 3. 与上次比较（pHash 完全相同 = 画面无变化）
        if self._prev_phash is not None and phash == self._prev_phash:
            # 无变化
            self._prev_phash = phash
            return

        # 有新变化
        self._prev_phash = phash
        self._change_count_since_last_alert += 1

        # 4. 首次变化立即提醒；持续变化时每 DEDUP_WINDOW 轮提醒一次
        should_alert = (
            self._change_count_since_last_alert >= self._dedup_window
            or self._event_history == []
        )

        if not should_alert:
            logger.debug(
                "桌面感知：检测到变化但未达提醒阈值 "
                "(%d/%d)",
                self._change_count_since_last_alert,
                self._dedup_window,
            )
            return

        # 5. AI 分析（复用本轮已采集的同一张截图，不重新截取）
        analysis = self._analyze_shot(shot)

        # 6. 构建事件
        event = DesktopAwarenessEvent(
            timestamp=time.monotonic(),
            description=analysis.get("text", "") or "检测到桌面变化",
            detail=analysis.get("text", ""),
            is_important=True,
            dedup_key=phash[:16] if phash else "",
            status="new",
            error=analysis.get("error"),
            worker_token=self._token,
        )

        # 7. 更新历史
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]

        # 8. 重置计数
        self._change_count_since_last_alert = 0

        # 9. 通知主线程（通过信号，Qt 自动排队到主线程）
        self.event_ready.emit(event)
        logger.info("桌面感知：新事件已生成")

    def _capture_screenshot(self) -> Optional[ScreenShot]:
        """
        调用 DesktopVision.capture() 获取内存截图。

        复用 desktop_vision.check_desktop_vision_authorization() 门禁（line 641），
        仅需 DV 开关开启，不需要 CC 总开关或批准计划。
        返回 ScreenShot 对象，授权拒绝或失败时返回 None。
        """
        try:
            from core.desktop_vision import DesktopVision

            vision = DesktopVision(ai_client=self._ai_client)
            shot = vision.capture()
            if shot.success:
                return shot
            logger.debug(
                "桌面感知：截图失败（%s）",
                shot.error or "未知原因",
            )
            return None
        except PermissionError as exc:
            logger.debug("桌面感知：授权拒绝（DV 关闭或未注册）：%s", exc)
            return None
        except Exception as exc:
            logger.debug("桌面感知：截图失败：%s", exc)
            return None

    @staticmethod
    def _compute_ahash(shot: ScreenShot) -> Optional[str]:
        """见模块级 _compute_ahash 函数。"""
        return _compute_ahash(shot)

    def _analyze_shot(self, shot: ScreenShot) -> dict[str, Any]:
        """
        对已采集的 ScreenShot 进行 AI 分析。

        ⚠️ 不调用 vision.capture()，直接使用传入的 shot 对象，
        避免同一次变化产生重复截图。

        每次分析前重新检查 DV 授权：用户在截图完成后关闭开关时，
        已缓存的截图不得继续上传云端。
        """
        try:
            from computer.desktop_access import check

            _dv_ok, _dv_reason = check()
            if not _dv_ok:
                logger.debug(
                    "桌面感知：分析阶段 DV 授权已关闭（%s），跳过上传。",
                    _dv_reason,
                )
                return {
                    "success": False,
                    "text": "",
                    "error": f"桌面视觉授权已关闭：{_dv_reason}",
                }

            from core.desktop_vision import DesktopVision

            vision = DesktopVision(ai_client=self._ai_client)

            result = vision.analyze(
                shot,
                question=(
                    "这是我当前看到的电脑桌面。"
                    "请用一两句话描述当前桌面上最重要的变化或活动。"
                    "如果画面没有明显变化，返回 '无明显变化'。"
                    "只描述事实，不推测意图。"
                ),
                user_confirmed=True,  # 内部使用，不对外暴露
            )

            return {
                "success": result.get("success", False),
                "text": result.get("text", ""),
                "error": result.get("error"),
            }

        except Exception as exc:
            logger.warning("桌面感知：AI 分析异常：%s", exc)
            return {
                "success": False,
                "text": "",
                "error": f"分析失败：{exc}",
            }


# ============================================================
# 对外接口：DesktopAwareness
# ============================================================

class DesktopAwareness:
    """
    桌面自主感知引擎 V1.1（修复版）。

    修复内容：
      - P0: 消除 resume()->start() 持锁递归死锁
      - P0: 修复 thread.wait(2000ms) 后无论线程是否退出都清空引用的问题
      - P1: 增加 worker_token 隔离旧线程延迟事件
      - P1: 补充 pHash 变化检测（替换仅校验前 4096 字节的 SHA-256）

    使用方式：
        awareness = DesktopAwareness(ai_client)
        awareness.on_alert = lambda event: print(event.summary())
        awareness.start()
        # ... later ...
        awareness.stop()

    状态机：
        STOPPED → RUNNING → PAUSED → RUNNING → STOPPED
    """

    def __init__(
        self,
        ai_client: Any,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        on_alert: Optional[Callable[[DesktopAwarenessEvent], None]] = None,
        on_state_change: Optional[Callable[[AwarenessState], None]] = None,
    ):
        """
        参数：
          ai_client        : AIClient 实例（用于截图和分析）
          interval_seconds : 采集间隔（秒），最小值 MIN_INTERVAL_SECONDS
          on_alert         : 事件回调（主线程调用）
          on_state_change  : 状态变化回调（主线程调用）
        """
        self._ai_client = ai_client
        self._interval = max(float(interval_seconds), MIN_INTERVAL_SECONDS)
        self._on_alert = on_alert
        self._on_state_change = on_state_change

        # 状态
        self._state = AwarenessState.STOPPED
        self._worker: Optional[_AwarenessWorker] = None
        self._thread: Optional[QThread] = None
        self._current_token: Optional[WorkerToken] = None
        self._generation_counter = 0
        self._lock = threading.Lock()

    # ── 公开属性 ────────────────────────────────────────────────

    @property
    def state(self) -> AwarenessState:
        """当前感知状态。"""
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state.is_running

    @property
    def is_paused(self) -> bool:
        return self._state.is_paused

    @property
    def is_stopped(self) -> bool:
        return self._state.is_stopped

    @property
    def interval_seconds(self) -> float:
        return self._interval

    @interval_seconds.setter
    def interval_seconds(self, value: float) -> None:
        self._interval = max(float(value), MIN_INTERVAL_SECONDS)
        if self._worker is not None:
            self._worker._interval = self._interval

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """
        启动桌面感知。

        前置条件：
          - DV 授权必须已开启（否则每轮截图会返回 None，不会报错）
          - 不会自动开启 CC 开关

        幂等：重复调用 start() 无害（已在运行中则静默忽略）。
        """
        with self._lock:
            if self._state.is_running:
                return  # 已在运行

            if self._state.is_paused:
                self._resume_worker()
                self._state = AwarenessState.RUNNING
                self._notify_state_change()
                return

            # 全新启动
            self._start_new_worker()
            self._state = AwarenessState.RUNNING
            self._notify_state_change()
            logger.info("桌面感知：已启动，间隔 %.1f 秒", self._interval)

    def pause(self) -> None:
        """
        暂停桌面感知。
        暂停期间不采集、不分析、不发送提醒。
        恢复后继续从当前位置开始。
        """
        with self._lock:
            if not self._state.is_running:
                return
            # 内联停止逻辑，避免再次持锁调用 stop()/stop_worker_silent()
            if self._worker is not None:
                try:
                    self._worker.stop_loop()
                except Exception:
                    pass
                self._worker = None

            if self._thread is not None:
                try:
                    self._thread.quit()
                    self._thread.wait(2000)
                except Exception:
                    pass
                finally:
                    # 与 stop() 保持一致：若线程仍未退出则保留引用，
                    # 防止 QThread 对象在 OS 线程仍在运行时被销毁。
                    if not self._thread.isRunning():
                        self._thread = None

            self._state = AwarenessState.PAUSED
            self._notify_state_change()
            logger.info("桌面感知：已暂停")

    def resume(self) -> None:
        """
        恢复桌面感知。
        - PAUSED 状态下重启现有 worker
        - STOPPED 状态下等价于 start()（不持锁递归调用 start()，避免死锁）
        """
        with self._lock:
            if self._state.is_running:
                return
            if self._state.is_stopped:
                # STOPPED → RUNNING：内联新建 worker 逻辑，
                # 避免在此持锁情况下递归调用 start()（会再次获取同一把锁导致死锁）
                self._start_new_worker()
                self._state = AwarenessState.RUNNING
                self._notify_state_change()
                logger.info("桌面感知：已从 STOPPED 恢复，间隔 %.1f 秒", self._interval)
                return
            if self._state.is_paused:
                self._resume_worker()
                self._state = AwarenessState.RUNNING
                self._notify_state_change()
                logger.info("桌面感知：已恢复")
                return

    def stop(self) -> None:
        """
        完全停止桌面感知并释放资源。
        停止后不可恢复，需重新调用 start()。
        """
        with self._lock:
            if self._worker is not None:
                try:
                    self._worker.stop_loop()
                except Exception:
                    pass
                self._worker = None

            if self._thread is not None:
                alive_before = self._thread.isRunning()
                try:
                    self._thread.quit()
                    self._thread.wait(2000)
                except Exception:
                    pass
                finally:
                    # 仅在确认线程真正退出后才清空引用
                    if self._thread.isRunning():
                        logger.warning(
                            "桌面感知：线程未在 2000ms 内退出，保留引用等待回收。"
                        )
                    else:
                        self._thread = None

            self._state = AwarenessState.STOPPED
            self._notify_state_change()
            logger.info("桌面感知：已停止")

    def status_report(self) -> dict[str, Any]:
        """
        返回当前状态的只读快照，供 UI 显示。
        """
        return {
            "state": self._state.value,
            "is_running": self._state.is_running,
            "is_paused": self._state.is_paused,
            "is_stopped": self._state.is_stopped,
            "interval_seconds": self._interval,
            "has_ai_client": self._ai_client is not None,
        }

    # ── 回调设置 ────────────────────────────────────────────────

    @property
    def on_alert(self) -> Optional[Callable[[DesktopAwarenessEvent], None]]:
        return self._on_alert

    @on_alert.setter
    def on_alert(self, value: Callable[[DesktopAwarenessEvent], None]) -> None:
        self._on_alert = value

    @property
    def on_state_change(self) -> Optional[Callable[[AwarenessState], None]]:
        return self._on_state_change

    @on_state_change.setter
    def on_state_change(self, value: Callable[[AwarenessState], None]) -> None:
        self._on_state_change = value

    # ── 内部实现 ────────────────────────────────────────────────

    def _start_new_worker(self) -> None:
        """创建新的 worker 线程并启动。"""
        # 分配新 token，标记新一轮启动
        self._generation_counter += 1
        new_token = WorkerToken(
            generation=self._generation_counter,
            worker_id=_next_worker_id(),
        )
        self._current_token = new_token

        self._worker = _AwarenessWorker(
            ai_client=self._ai_client,
            interval_seconds=self._interval,
            token=new_token,
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # 连接信号：worker 事件 → 主线程回调
        self._worker.event_ready.connect(self._on_event_from_worker)

        # 连接线程生命周期
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

        # 清理引用（异常时帮助 GC）
        self._thread._awareness_ref = self  # type: ignore[attr-defined]

        self._thread.start()
        logger.info(
            "桌面感知：新 worker 已创建 [gen=%d, id=%d]",
            new_token.generation, new_token.worker_id,
        )

    def _resume_worker(self) -> None:
        """
        恢复已暂停的 worker（重启循环，保留状态）。

        ⚠️ 由于 Qt 不允许在同一 QObject 已被 moveToThread 的旧线程上
        再次 moveToThread，这里总是创建全新 worker 和线程。
        仅重启 _cycle 状态（pHash、历史等）。
        """
        # 释放旧 worker 及线程资源（若有）
        if self._worker is not None:
            try:
                self._worker.stop_loop()
            except Exception:
                pass
            self._worker = None
        if self._thread is not None:
            try:
                self._thread.quit()
                self._thread.wait(2000)
            except Exception:
                pass
            finally:
                if not (self._thread.isRunning() if self._thread else False):
                    self._thread = None

        # 生成新 token（即使是"恢复"也视为新启动轮次）
        self._generation_counter += 1
        new_token = WorkerToken(
            generation=self._generation_counter,
            worker_id=_next_worker_id(),
        )
        self._current_token = new_token

        self._worker = _AwarenessWorker(
            ai_client=self._ai_client,
            interval_seconds=self._interval,
            token=new_token,
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.event_ready.connect(self._on_event_from_worker)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread._awareness_ref = self  # type: ignore[attr-defined]
        self._thread.start()
        logger.info(
            "桌面感知：恢复 worker [gen=%d, id=%d]",
            new_token.generation, new_token.worker_id,
        )

    def _on_event_from_worker(self, event: DesktopAwarenessEvent) -> None:
        """
        在工作线程中通过 Qt 信号触发，自动路由到主线程执行。
        在此方法内调用用户回调。

        守卫逻辑：
          1. stop() 后 _state.is_stopped=True → 丢弃（覆盖 stop 后所有延迟信号）
          2. worker_token 不匹配 → 丢弃（覆盖 old-worker 在新实例启动后的延迟信号）
        """
        # 守卫 1：已停止状态
        if self._state.is_stopped:
            logger.debug(
                "桌面感知：事件被丢弃（已停止）gen=%d",
                event.worker_token.generation if event.worker_token else -1,
            )
            return

        # 守卫 2：worker_token 匹配（隔离旧线程事件）
        if (
            self._current_token is not None
            and event.worker_token is not None
            and event.worker_token.generation != self._current_token.generation
        ):
            logger.debug(
                "桌面感知：事件被丢弃（旧 worker 延迟）"
                "event_gen=%d current_gen=%d",
                event.worker_token.generation,
                self._current_token.generation,
            )
            return

        try:
            if self._on_alert is not None:
                self._on_alert(event)
        except Exception as exc:
            logger.warning("桌面感知：on_alert 回调异常：%s", exc)

    def _notify_state_change(self) -> None:
        """通知主线程状态变化。"""
        try:
            if self._on_state_change is not None:
                self._on_state_change(self._state)
        except Exception as exc:
            logger.warning("桌面感知：on_state_change 回调异常：%s", exc)

    # ── 析构保护 ────────────────────────────────────────────────

    def __del__(self) -> None:
        """确保析构时停止所有后台活动。"""
        try:
            self.stop()
        except Exception:
            pass
