"""AppContext — 进程级共享服务底座（Phase 0 最小集）。

设计目标（授权 §三 T4）：
  * 一个共享 AIClient 实例：L1（ui.py）与 L2（spatial_ui/）共用同一会话、
    同一记忆、同一工具注册表、同一桌面视觉授权门禁。
  * 一个共享 SettingsManager 实例：单写者语义，避免双写竞态。
  * 一个共享 ai_busy 状态：L1/L2 两个 UI 各自的本地 _ai_busy 无法跨 UI 互斥
    （双实例时各持一个 flag）；共享状态提供"单活动请求"跨 UI 互斥。

生命周期约定：
  * 进程内唯一 AppContext；get() 幂等（重复调用返回同一实例）。
  * 无 destroy；进程退出即释放。
  * 测试可经 _reset_for_tests() 丢弃实例重构造。

线程安全边界：
  * get() 用 threading.Lock 双检，跨线程安全。
  * ai_busy 用 threading.Lock 做原子抢占（acquire(blocking=False)），
    由 Qt worker 的 finished/error 信号回主线程后 release；未来非 Qt
    调用方（语音路由等）同样可用该互斥，语义一致。

导入卫生（关键）：
  * 本模块【不】在顶层 import core.ai_client / core.settings。
    原因：core.ai_client.__init__ 会先 import 整个 core 包链
    （computer_controller → core.settings → keyring / openai …），
    而 services/app_context 由 ui.py / spatial_main_window.py 在模块
    顶层被 import；若在 services 层再顶层拉 core.settings，
    会与其它测试文件的 import 顺序形成循环导入风险（pytest 收集期
    表现为 test_image_generation / test_media_generation 的 SettingsManager
    类属性被污染，temp_file.replace 抛 PermissionError）。
    因此两个重依赖都在 _init() 内延迟 import，import services.app_context
    本身零副作用（仅 stdlib）。

Phase 0 明确【不】做（授权 §三 硬约束）：
  * 不创建纯转发门面（ai_service / settings_service / ...）；
  * 不引入 PermissionService / BrowserService / TaskService /
    VoiceCommandRouter（尚无实际消费者）；
  * 不修改 core/ 任何文件（AIClient 自带 SettingsManager；单实例后 L1/L2
    共享同一 ai_client.settings，无独立 SettingsManager 双写）。
"""

from __future__ import annotations

import threading
from typing import Optional


class AppContext:
    """进程级单例：持有共享 AIClient / SettingsManager / ai_busy 互斥。"""

    _instance: Optional["AppContext"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # 延迟到 _init() 填充，避免构造即拉起重资源
        self._initialized = False
        # 运行时类型：core.ai_client.AIClient / core.settings.SettingsManager
        # （不在模块顶层 import，见 docstring「导入卫生」）
        self._ai_client = None
        self._settings = None
        # 跨 UI 单活动请求互斥：占用即"有请求在途"
        self._ai_busy_lock = threading.Lock()

    # ── 单例 ─────────────────────────────────────────────

    @classmethod
    def get(cls) -> "AppContext":
        """幂等获取进程级 AppContext（双检锁，跨线程安全）。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    ctx = cls()
                    ctx._init()
                    cls._instance = ctx
        return cls._instance

    def _init(self) -> None:
        """首次初始化：构造共享 AIClient / SettingsManager。

        AIClient 构造会注册桌面视觉授权门禁（core/ai_client.py
        set_desktop_vision_authorization, allow_replace=True）。单实例化后
        该门禁只注册一次，避免 L1/L2 双实例重复注册竞态。

        延迟 import（见 docstring「导入卫生」）：此处才真正拉起重依赖。
        """
        from core.ai_client import AIClient

        # AIClient 在 __init__ 内自建 SettingsManager（core/ai_client.py
        # L356）。进程内只保留这一个实例并对外暴露为共享 settings：
        # L1 的 SettingsDialog 经 ai_client.settings 读写，L2 构造器
        # 接收的 settings 也指向它 —— 单写者语义，无需二次构造。
        self._ai_client = AIClient()
        self._settings = self._ai_client.settings

        self._initialized = True

    # ── 访问器 ───────────────────────────────────────────

    @property
    def ai_client(self):
        """共享 AIClient（L1/L2 唯一实例）。"""
        if not self._initialized:
            raise RuntimeError("AppContext 未初始化：先调用 AppContext.get()")
        if self._ai_client is None:
            raise RuntimeError("AppContext.ai_client 缺失（_init 未成功）")
        return self._ai_client

    @property
    def settings(self):
        """共享 SettingsManager（单写者）。"""
        if not self._initialized:
            raise RuntimeError("AppContext 未初始化：先调用 AppContext.get()")
        if self._settings is None:
            raise RuntimeError("AppContext.settings 缺失（_init 未成功）")
        return self._settings

    # ── 共享 ai_busy（跨 UI 单活动请求互斥）──────────────

    @property
    def ai_busy(self) -> bool:
        """当前是否有 AI 请求在途（跨 L1/L2 共享）。

        非阻塞探测：另一线程正持有互斥锁时为 True。
        """
        return self._ai_busy_lock.locked()

    def acquire_ai_busy(self, owner: str = "") -> bool:
        """原子抢占 ai_busy；已有请求在途返回 False（另一 UI 占用）。

        owner 仅用于诊断/测试断言，不强制。
        """
        return self._ai_busy_lock.acquire(blocking=False)

    def release_ai_busy(self) -> None:
        """释放 ai_busy（请求完成/失败时调用，须与 acquire 成对）。

        未持有时无操作（幂等释放，避免误报异常）。
        """
        try:
            self._ai_busy_lock.release()
        except RuntimeError:
            # 未持有（从未 acquire 或已释放）→ 幂等忽略
            pass

    # ── 生命周期校验 ─────────────────────────────────────

    def assert_ready(self) -> None:
        """供测试/启动自检：确认共享对象已就位。"""
        if not self._initialized:
            raise RuntimeError("AppContext 未初始化")
        if self._ai_client is None or self._settings is None:
            raise RuntimeError("AppContext 共享对象缺失")

    @classmethod
    def _reset_for_tests(cls) -> None:
        """测试专用：丢弃当前实例，使下次 get() 重新构造。

        ⚠️ 仅测试使用。生产路径不应调用。
        """
        with cls._instance_lock:
            cls._instance = None
