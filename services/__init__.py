"""services 包 — 进程级共享服务底座（最小集）。

Phase 0 授权范围（任务书 §三 T4）：
仅建立必要的进程级共享对象：一个 AIClient、一个 SettingsManager、
一个共享 ai_busy 状态、L1/L2 统一注入入口。

不创建纯转发门面；不实施尚无实际消费者的 PermissionService /
BrowserService / TaskService / VoiceCommandRouter。
"""

from services.app_context import AppContext

__all__ = ["AppContext"]
