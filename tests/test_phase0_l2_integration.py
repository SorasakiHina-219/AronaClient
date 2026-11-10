"""Phase 0 · T4 集成测试 —— L2 正式链路（真实 AIClient + 共享 AppContext）。

验证目标（授权 §三 T1/T4、§六）：
  * L2 聊天走正式 AIClient.send_message（D4），不再依赖 Preview mock；
  * L2 能收到并显示实际响应（将回复写入聊天区，而非仅起线程）；
  * L2 new_chat 真正调用 create_conversation（D3），会话数 +1；
  * L2 与 AppContext 共享同一 AIClient / SettingsManager；
  * 请求完成后共享 ai_busy 释放。

离线约束：不配置 API Key、不发网络。以 fake OpenAI backend 替换
ai_client.client（仅替换底层传输对象，send_message/记忆/会话代码均为真实
AIClient 代码），并把 USERPROFILE 指向 tmp_path，避免写入用户数据。

Run:
  QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_phase0_l2_integration.py -v
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """隔离的 AppContext：临时 home + 临时 settings + 单例重置。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from core.settings import SettingsManager

    old_dir, old_file = SettingsManager.SETTINGS_DIR, SettingsManager.SETTINGS_FILE
    SettingsManager.SETTINGS_DIR = tmp_path
    SettingsManager.SETTINGS_FILE = tmp_path / "settings.json"

    from services.app_context import AppContext
    AppContext._reset_for_tests()
    instance = AppContext.get()
    try:
        yield instance
    finally:
        AppContext._reset_for_tests()
        SettingsManager.SETTINGS_DIR = old_dir
        SettingsManager.SETTINGS_FILE = old_file


def _fake_backend(reply="测试回复：你好，指挥官。"):
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=reply, tool_calls=None),
                )]
            )

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    return _Client(), calls


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout=10.0):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_l2_real_aiclient_chat_end_to_end(ctx, qapp):
    """L2 发送 -> 真实 AIClient.send_message -> 回复显示 -> busy 释放。"""
    from spatial_ui.spatial_main_window import SpatialMainWindow

    ai = ctx.ai_client
    fake, calls = _fake_backend()
    ai.client = fake

    win = SpatialMainWindow(ai_client=ai, settings=ctx.settings)
    assert win.ai_client is ai
    assert win.settings is ctx.settings

    displayed = []
    original = win.add_message

    def spy(role, text, *args, **kwargs):
        displayed.append((role, text))
        return original(role, text, *args, **kwargs)

    win.add_message = spy

    win._on_message_sent("你好")
    assert _wait_until(lambda: not win._ai_busy), "L2 请求未在超时内完成"

    assert calls, "正式 AIClient.send_message 未发出请求"
    assert calls[0]["model"] == ai.model
    roles = [r for r, _ in displayed]
    assert roles[0] == "user"
    assert ("assistant", "测试回复：你好，指挥官。") in displayed, displayed
    assert ctx.ai_busy is False, "请求完成后共享 ai_busy 必须释放"

    win.close()


def test_l2_new_chat_creates_conversation(ctx, qapp):
    """D3：new_chat 走 create_conversation 正路，会话数 +1 且当前会话变化。"""
    from spatial_ui.spatial_main_window import SpatialMainWindow

    ai = ctx.ai_client
    win = SpatialMainWindow(ai_client=ai, settings=ctx.settings)

    before = list(ai.list_conversations())
    before_id = ai.get_current_session().get("id")
    win.new_chat()
    after = list(ai.list_conversations())

    assert len(after) == len(before) + 1, (before, after)
    assert ai.get_current_session().get("id") != before_id

    win.close()


def test_app_context_shared_instances_are_the_ui_dependency(ctx, qapp):
    """L1/L2 统一注入入口：AppContext 提供的就是唯一 AIClient。"""
    from spatial_ui.spatial_main_window import SpatialMainWindow

    ai = ctx.ai_client
    assert ctx.settings is ai.settings

    win = SpatialMainWindow(ai_client=ai, settings=ctx.settings)
    assert win.ai_client is ai
    assert win.settings is ai.settings
    win.close()
