
"""AronaClient V0.2 — 只读桌面感知工具与 AI 调用链测试。

覆盖：
    1. 桌面感知工具是否真实注册进 ToolRegistry
    2. 工具 schema 是否符合 OpenAI function calling 协议
    3. 只读工具的最小必要字段（不泄露句柄 / 完整路径）
    4. 未授权时 fail-closed，不伪造桌面状态
    5. 参数校验（limit 类型 / 范围）
    6. AI 工具调用循环（mock 客户端，无真实 API）
    7. 工具调用轮数上限
    8. 会话隔离（tool 消息不进入会话历史）
    9. 4000 字符截断仍生效
   10. 只读工具与有副作用工具的权限边界

所有测试使用 mock，不进行真实桌面操作，不发送真实 API 请求。
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _tool_call(call_id, name, arguments):
    """构造 OpenAI 风格的 tool_call 对象。"""
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(content=None, tool_calls=None):
    """构造 OpenAI 风格的 chat completion 响应。"""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _make_client(registry, side_effect):
    """构造一个不联网的 AIClient 替身。"""
    from core.ai_client import AIClient

    with patch.object(AIClient, "__init__", lambda self: None):
        client = AIClient()

    client.messages = []
    client.current_session = None
    client.memory = MagicMock()
    client.memory.build_memory_context.return_value = ""
    client.memory.auto_remember = MagicMock()
    client._tool_registry = registry
    client._select_model = lambda has_visual: "test-model"
    client.settings = MagicMock()
    client.settings.get_api_key.return_value = "test-key"

    fake = MagicMock()
    fake.chat.completions.create.side_effect = side_effect
    client.client = fake

    return client, fake


class TestPerceptionToolsRegistered(unittest.TestCase):
    """桌面感知工具必须真实注册，AI 才能调用。"""

    def setUp(self):
        import computer.desktop_access as da
        from computer.tool_registry import ToolRegistry

        da.set_provider(
            lambda: (True, "测试授权"), allow_replace=True
        )
        self.registry = ToolRegistry()

    def test_readonly_tools_registered(self):
        """只读桌面感知工具必须存在。"""
        self.assertTrue(self.registry.has_tool("get_active_window"))
        self.assertTrue(self.registry.has_tool("list_windows"))

    def test_side_effect_tools_still_registered(self):
        """有副作用工具不得因新增只读工具而丢失。"""
        self.assertTrue(self.registry.has_tool("open_url"))
        self.assertTrue(self.registry.has_tool("launch_app"))
        self.assertTrue(self.registry.has_tool("download_file"))

    def test_total_tool_count(self):
        """工具总数应为 5（3 个副作用 + 2 个只读）。"""
        self.assertEqual(self.registry.tool_count(), 5)

    def test_schema_openai_compatible(self):
        """所有工具 schema 必须符合 OpenAI function calling 格式。"""
        for schema in self.registry.get_tools():
            self.assertEqual(schema["type"], "function")
            fn = schema["function"]
            self.assertIsInstance(fn["name"], str)
            self.assertTrue(fn["name"])
            self.assertIsInstance(fn["description"], str)
            self.assertEqual(fn["parameters"]["type"], "object")
            self.assertIn("properties", fn["parameters"])

    def test_get_active_window_takes_no_params(self):
        """get_active_window 无参数。"""
        schema = next(
            s for s in self.registry.get_tools()
            if s["function"]["name"] == "get_active_window"
        )
        self.assertEqual(
            schema["function"]["parameters"]["properties"], {}
        )
        self.assertEqual(
            schema["function"]["parameters"]["required"], []
        )

    def test_list_windows_limit_param_declared(self):
        """list_windows 声明 limit 参数。"""
        schema = next(
            s for s in self.registry.get_tools()
            if s["function"]["name"] == "list_windows"
        )
        props = schema["function"]["parameters"]["properties"]
        self.assertIn("limit", props)
        self.assertEqual(props["limit"]["type"], "integer")


class TestReadonlyToolResults(unittest.TestCase):
    """只读工具返回值必须结构清晰且符合最小必要原则。"""

    def setUp(self):
        import computer.desktop_access as da
        from computer.tool_registry import ToolRegistry

        da.set_provider(
            lambda: (True, "测试授权"), allow_replace=True
        )
        self.registry = ToolRegistry()

    def test_get_active_window_result_shape(self):
        """活动窗口结果包含模型需要的字段。"""
        result = self.registry.call_tool("get_active_window", {})
        self.assertIn("success", result)
        self.assertIn("result", result)
        self.assertIn("error", result)
        self.assertIn("message", result)
        self.assertIsInstance(result["message"], str)

    def test_get_active_window_no_internal_identifiers(self):
        """活动窗口结果不得暴露窗口句柄与完整进程路径。"""
        result = self.registry.call_tool("get_active_window", {})
        if not result["success"]:
            self.skipTest("当前环境无法读取活动窗口")

        payload = result["result"]
        self.assertNotIn("window_handle", payload)
        self.assertNotIn("process_path", payload)
        self.assertNotIn("process_id", payload)
        self.assertIn("process_name", payload)

    def test_list_windows_result_shape(self):
        """窗口列表结果是 list，元素为 dict。"""
        result = self.registry.call_tool("list_windows", {"limit": 5})
        self.assertIn("success", result)
        if not result["success"]:
            self.skipTest("当前环境无法枚举窗口")

        self.assertIsInstance(result["result"], list)
        for item in result["result"]:
            self.assertIsInstance(item, dict)
            self.assertIn("title", item)

    def test_list_windows_no_internal_identifiers(self):
        """窗口列表不得暴露句柄与进程路径。"""
        result = self.registry.call_tool("list_windows", {"limit": 5})
        if not result["success"]:
            self.skipTest("当前环境无法枚举窗口")

        for item in result["result"]:
            self.assertNotIn("window_handle", item)
            self.assertNotIn("process_path", item)
            self.assertNotIn("process_id", item)

    def test_list_windows_respects_limit(self):
        """limit 必须被遵守。"""
        result = self.registry.call_tool("list_windows", {"limit": 2})
        if not result["success"]:
            self.skipTest("当前环境无法枚举窗口")
        self.assertLessEqual(len(result["result"]), 2)


class TestReadonlyToolUnauthorized(unittest.TestCase):
    """未授权时必须 fail-closed，不得伪造桌面状态。"""

    def setUp(self):
        import computer.desktop_access as da
        from computer.tool_registry import ToolRegistry

        da.clear_provider()
        self.registry = ToolRegistry()

    def tearDown(self):
        import computer.desktop_access as da
        da.clear_provider()

    def test_get_active_window_denied(self):
        """未授权时 get_active_window 返回失败且无数据。"""
        result = self.registry.call_tool("get_active_window", {})
        self.assertFalse(result["success"])
        self.assertIsNone(result["result"])
        self.assertTrue(result["error"])

    def test_list_windows_denied(self):
        """未授权时 list_windows 返回失败且无数据。"""
        result = self.registry.call_tool("list_windows", {})
        self.assertFalse(result["success"])
        self.assertIsNone(result["result"])
        self.assertTrue(result["error"])


class TestReadonlyToolParamValidation(unittest.TestCase):
    """参数校验。"""

    def setUp(self):
        import computer.desktop_access as da
        from computer.tool_registry import ToolRegistry

        da.set_provider(
            lambda: (True, "测试授权"), allow_replace=True
        )
        self.registry = ToolRegistry()

    def test_limit_exceeds_max_rejected(self):
        """limit 超过上限应返回明确失败。"""
        result = self.registry.call_tool("list_windows", {"limit": 999})
        self.assertFalse(result["success"])
        self.assertIsNone(result["result"])
        self.assertIn("200", result["message"])

    def test_limit_zero_rejected(self):
        """limit=0 应被拒绝。"""
        result = self.registry.call_tool("list_windows", {"limit": 0})
        self.assertFalse(result["success"])
        self.assertIsNone(result["result"])

    def test_limit_bool_rejected(self):
        """bool 不得被当作整数接受。"""
        result = self.registry.call_tool("list_windows", {"limit": True})
        self.assertFalse(result["success"])

    def test_limit_string_rejected(self):
        """字符串 limit 应被拒绝。"""
        result = self.registry.call_tool("list_windows", {"limit": "5"})
        self.assertFalse(result["success"])

    def test_unknown_tool_rejected(self):
        """未知工具必须被拒绝。"""
        result = self.registry.call_tool("format_disk", {})
        self.assertFalse(result["success"])
        self.assertIn("未知工具", result["error"])


class TestAIToolCallLoop(unittest.TestCase):
    """AI 工具调用循环（mock 客户端，无真实 API）。"""

    def setUp(self):
        import computer.desktop_access as da
        from computer.tool_registry import ToolRegistry

        da.set_provider(
            lambda: (True, "测试授权"), allow_replace=True
        )
        self.registry = ToolRegistry()

    def test_tool_call_round_trip(self):
        """模型请求工具 -> 执行 -> 结果回传 -> 最终回答。"""
        client, fake = _make_client(self.registry, [
            _response(tool_calls=[
                _tool_call("call_1", "list_windows",
                           json.dumps({"limit": 2}))
            ]),
            _response(content="我看到 2 个窗口。"),
        ])

        answer = client.send_message("看看桌面上有哪些窗口")

        self.assertEqual(answer, "我看到 2 个窗口。")
        self.assertEqual(fake.chat.completions.create.call_count, 2)

    def test_tool_message_sent_back_to_model(self):
        """工具结果必须以 role=tool 回传给模型。"""
        client, fake = _make_client(self.registry, [
            _response(tool_calls=[
                _tool_call("call_1", "get_active_window", "{}")
            ]),
            _response(content="完成"),
        ])

        client.send_message("当前窗口是什么")

        # 第二次调用的 messages 中应包含 role=tool
        second_call = fake.chat.completions.create.call_args_list[1]
        messages = second_call.kwargs["messages"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertTrue(tool_msgs, "工具结果必须回传给模型")
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_1")

    def test_tool_messages_not_in_session_history(self):
        """tool 消息不得写入会话历史。"""
        client, _ = _make_client(self.registry, [
            _response(tool_calls=[
                _tool_call("call_1", "list_windows", "{}")
            ]),
            _response(content="完成"),
        ])

        client.send_message("列出窗口")

        roles = [m.get("role") for m in client.messages]
        self.assertNotIn("tool", roles)
        self.assertEqual(roles, ["user", "assistant"])

    def test_tool_failure_reported_to_model(self):
        """工具失败时模型收到明确的失败信息。"""
        client, fake = _make_client(self.registry, [
            _response(tool_calls=[
                _tool_call("call_1", "nonexistent_tool", "{}")
            ]),
            _response(content="这个工具不存在。"),
        ])

        client.send_message("调用一个不存在的工具")

        second_call = fake.chat.completions.create.call_args_list[1]
        messages = second_call.kwargs["messages"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertTrue(tool_msgs)
        self.assertIn("不存在", tool_msgs[0]["content"])

    def test_max_rounds_limit(self):
        """模型无限请求工具时，循环必须在 5 轮内停止。"""
        # 每轮都返回工具调用，永不给出最终回答
        infinite = [
            _response(tool_calls=[
                _tool_call(f"call_{i}", "list_windows", "{}")
            ])
            for i in range(10)
        ]
        client, fake = _make_client(self.registry, infinite)

        answer = client.send_message("一直调用工具")

        self.assertLessEqual(fake.chat.completions.create.call_count, 5)
        # 用尽轮数时没有最终回答，不应伪造内容
        self.assertEqual(answer, "")

    def test_tool_result_truncated_to_4000(self):
        """超长工具结果最终消息总长度必须 <= 4000（截断标记计入总长）。"""
        huge = "x" * 10000

        def fake_handler(**_kwargs):
            return {
                "success": True,
                "result": None,
                "error": None,
                "message": huge,
            }

        self.registry.register(
            schema={
                "type": "function",
                "function": {
                    "name": "huge_tool",
                    "description": "returns huge text",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            handler=fake_handler,
        )

        client, fake = _make_client(self.registry, [
            _response(tool_calls=[
                _tool_call("call_1", "huge_tool", "{}")
            ]),
            _response(content="完成"),
        ])

        client.send_message("测试截断")

        second_call = fake.chat.completions.create.call_args_list[1]
        messages = second_call.kwargs["messages"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        content = tool_msgs[0]["content"]

        # 硬上限：最终总长 <= 4000（前缀 + 正文 + 截断标记均计入，不超 4000）
        self.assertLessEqual(len(content), 4000)
        self.assertTrue(content.endswith("...[truncated]"))

    def test_malformed_json_arguments_tolerated(self):
        """非法 JSON 参数不应让循环崩溃。"""
        client, fake = _make_client(self.registry, [
            _response(tool_calls=[
                _tool_call("call_1", "list_windows", "{not json")
            ]),
            _response(content="参数有问题。"),
        ])

        answer = client.send_message("坏参数")

        self.assertEqual(answer, "参数有问题。")
        self.assertEqual(fake.chat.completions.create.call_count, 2)


class TestSessionMessageIntegrity(unittest.TestCase):
    """会话消息完整性：不得重复发送同一条用户消息。"""

    def setUp(self):
        import computer.desktop_access as da
        from computer.tool_registry import ToolRegistry

        da.set_provider(
            lambda: (True, "测试授权"), allow_replace=True
        )
        self.registry = ToolRegistry()

    def test_user_message_sent_once(self):
        """save_to_session=True 时用户消息只发送一次。"""
        client, fake = _make_client(
            self.registry, [_response(content="好的")]
        )

        client.send_message("你好")

        messages = fake.chat.completions.create.call_args.kwargs["messages"]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        self.assertEqual(
            len(user_msgs), 1,
            "同一条用户消息不得重复发送给模型",
        )
        self.assertEqual(user_msgs[0]["content"], "你好")

    def test_history_included_after_two_turns(self):
        """第二轮请求应包含历史，且每轮消息各出现一次。"""
        client, fake = _make_client(self.registry, [
            _response(content="第一轮回答"),
            _response(content="第二轮回答"),
        ])

        client.send_message("第一轮提问")
        client.send_message("第二轮提问")

        messages = fake.chat.completions.create.call_args_list[1].kwargs["messages"]
        contents = [m.get("content") for m in messages]

        self.assertEqual(contents.count("第一轮提问"), 1)
        self.assertEqual(contents.count("第一轮回答"), 1)
        self.assertEqual(contents.count("第二轮提问"), 1)

    def test_no_session_uses_only_current_message(self):
        """save_to_session=False 时不带历史。"""
        client, fake = _make_client(
            self.registry, [_response(content="感知回答")]
        )

        client.send_message("后台感知请求", save_to_session=False)

        messages = fake.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "后台感知请求")
        # 且不写入会话历史
        self.assertEqual(client.messages, [])


class TestPermissionBoundary(unittest.TestCase):
    """只读工具与有副作用工具的权限边界。"""

    def test_readonly_tools_have_no_side_effects(self):
        """只读工具源码不得包含鼠标/键盘/进程启动调用。"""
        import inspect
        from computer.tool_registry import ToolRegistry

        for method_name in ("_get_active_window_tool", "_list_windows_tool"):
            source = inspect.getsource(
                getattr(ToolRegistry, method_name)
            )
            for forbidden in (
                "pyautogui", "subprocess", "os.system",
                "webbrowser", "Popen", "click(", "typewrite",
            ):
                self.assertNotIn(
                    forbidden, source,
                    f"{method_name} 不应包含副作用调用：{forbidden}",
                )

    def test_readonly_gate_uses_desktop_access(self):
        """只读工具必须经过 desktop_access 门禁（不自行放行）。"""
        import inspect
        from computer import window_info

        for fn in (window_info.get_active_window,
                   window_info.list_visible_windows):
            source = inspect.getsource(fn)
            self.assertIn("_access_check", source)
            self.assertIn("_note_denied", source)

    def test_side_effect_tools_keep_own_guards(self):
        """有副作用工具各自的安全校验仍然存在。"""
        import inspect
        from computer.app_launcher import AppLauncher
        from computer.web_browser import BrowserController
        from computer.downloader import Downloader

        self.assertNotIn(
            "shell=True", inspect.getsource(AppLauncher.launch)
        )
        self.assertIn(
            "ALLOWED_SCHEMES",
            inspect.getsource(BrowserController._validate_url),
        )
        self.assertIn(
            "_resolve_and_validate_dns",
            inspect.getsource(Downloader.download),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
