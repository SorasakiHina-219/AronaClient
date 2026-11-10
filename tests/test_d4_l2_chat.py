"""T1 (D4 + D3) 验证测试 — L2 聊天调用修复。

证明：
  1. 修复前根因：L2 曾调用 AIClient.chat() / AIClient.new_session()，
     二者在正式 AIClient 上均不存在（仅预览 mock 有），故正式入口必 AttributeError。
  2. 修复后：L2 _AIWorker 改调 send_message，L2 new_chat 改调 create_conversation，
     均命中 AIClient 真实 API。
  3. 预览 mock 提供 send_message / create_conversation 别名，与正式入口共用同一套 L2 业务代码。

本测试不启动 QApplication、不发网络、不触桌面：以 stub client 驱动 L2 _AIWorker，
并校验 AIClient 的 API 表面。

Run:
  .venv/Scripts/python.exe -m pytest tests/test_d4_l2_chat.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── 根因证明：正式 AIClient 无 chat / new_session ─────────────────────

def test_aichat_no_chat_method():
    """D4 根因：L2 旧调用 self.ai_client.chat() 在正式 AIClient 上不存在。"""
    from core.ai_client import AIClient
    assert not hasattr(AIClient, "chat"), (
        "AIClient 不应有 chat()；chat 仅存在于预览 mock"
    )


def test_aichat_no_new_session_method():
    """D3 根因：L2 旧调用 self.ai_client.new_session() 在正式 AIClient 上不存在。"""
    from core.ai_client import AIClient
    assert not hasattr(AIClient, "new_session"), (
        "AIClient 不应有 new_session()；真实 API 为 create_conversation / clear_history"
    )


def test_aichat_has_real_api():
    """L2 修复后依赖的 AIClient 真实公共方法确实存在。"""
    from core.ai_client import AIClient
    for m in ("send_message", "create_conversation", "clear_history",
              "list_conversations", "switch_conversation", "get_current_session"):
        assert hasattr(AIClient, m), f"AIClient 缺少真实方法 {m}"


# ── 修复后行为：L2 _AIWorker 走 send_message ─────────────────────────

class _StubClient:
    """最小 AIClient 替身：仅暴露 L2 _AIWorker 用到的 send_message。"""

    def __init__(self, reply="hello-from-real-ai"):
        self.reply = reply
        self.calls = []

    def send_message(self, message, attachments=None, save_to_session=True):
        self.calls.append((message, attachments, save_to_session))
        return self.reply


class _BrokenClient:
    """模拟修复前：只有 chat（mock 风格），无 send_message。"""

    def chat(self, message):
        return "mock"


def _run_worker(client, text):
    """驱动 L2 _AIWorker 的 run()（不依赖 Qt 事件循环，直接调用槽函数）。"""
    from PySide6.QtCore import QObject
    from spatial_ui.spatial_main_window import _AIWorker

    worker = _AIWorker(client, text)

    finished, errored = [], []
    # 手动接管信号（不启动线程 / 事件循环）
    worker.finished.connect(lambda v: finished.append(v))
    worker.error.connect(lambda v: errored.append(v))
    worker.run()
    return finished, errored, worker


def test_l2_worker_uses_send_message():
    client = _StubClient(reply="你好，我在")
    finished, errored, _ = _run_worker(client, "hi")
    assert errored == [], f"不应报错，实际：{errored}"
    assert finished and finished[0] == "你好，我在"
    assert client.calls == [("hi", None, True)]


def test_l2_worker_error_path_is_attribute_error_free():
    """修复前用 _BrokenClient（仅 chat）：L2 旧代码会 AttributeError。
    修复后 L2 调 send_message → _BrokenClient 无该方法 → 走 error 信号，
    而非'静默成功'，证明调用点已真正切换。"""
    broken = _BrokenClient()
    finished, errored, _ = _run_worker(broken, "hi")
    assert finished == [], "不应有成功结果"
    assert len(errored) == 1
    assert "send_message" in errored[0] or "has no attribute" in errored[0]


# ── new_chat → create_conversation ───────────────────────────────────

def test_new_chat_calls_create_conversation():
    """D3：L2 new_chat 应调 create_conversation（真实新建会话），而非 new_session。"""
    created = []

    class Client:
        def create_conversation(self, title="新对话"):
            created.append(title)
            return {"id": "s_new", "title": title}

        def clear_history(self):
            created.append("clear_fallback")

    import spatial_ui.spatial_main_window as smw
    instance = smw.SpatialMainWindow.__new__(smw.SpatialMainWindow)
    instance.ai_client = Client()
    instance._clear_chat = lambda: None
    instance.switch_page = lambda *a, **k: None
    instance.home_page = type("H", (), {"set_status_text": lambda s, t: None})()
    instance._refresh_history = lambda: None

    instance.new_chat()
    assert created and created[0] == "新对话", f"应走 create_conversation，实际：{created}"
    assert "clear_fallback" not in created, "不应回退到 clear_history"
