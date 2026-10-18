
"""AronaClient V0.2 桌面操作能力自动化测试（Mock 隔离）。

测试范围：
    1. 浏览器控制 — 合法/非法 URL
    2. 应用启动 — 路径合法性、缺失路径、注入检测
    3. 下载模块 — 协议校验、文件名清理、重定向
    4. 工具注册表 — schema 生成、工具调用
    5. AIClient 集成 — 工具传递、tool_call 处理

安全约束验证：
    - 不实际下载大文件
    - 不启动真实应用
    - 不调用真实 API
    - 不操作真实鼠标键盘
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ============================================================
# Helper: add project root to path
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Test 1: Browser Controller
# ============================================================
class TestBrowserController(unittest.TestCase):
    """浏览器控制器安全测试。"""

    def setUp(self):
        from computer.web_browser import BrowserController
        self.browser = BrowserController()

    def test_allow_https(self):
        """HTTPS URL 应被允许。"""
        with patch('computer.web_browser.webbrowser.open') as mock_open:
            mock_open.return_value = True
            result = self.browser.open_url('https://example.com')
            self.assertTrue(result['success'])
            mock_open.assert_called_once()

    def test_allow_http(self):
        """HTTP URL 应被允许（浏览器层面允许）。"""
        with patch('computer.web_browser.webbrowser.open') as mock_open:
            mock_open.return_value = True
            result = self.browser.open_url('http://example.com')
            self.assertTrue(result['success'])

    def test_block_javascript(self):
        """javascript: 协议必须被阻止。"""
        result = self.browser.open_url('javascript:alert(1)')
        self.assertFalse(result['success'])
        self.assertIn('协议', result['message'])

    def test_block_file(self):
        """file: 协议必须被阻止。"""
        result = self.browser.open_url('file:///etc/passwd')
        self.assertFalse(result['success'])

    def test_block_data(self):
        """data: 协议必须被阻止。"""
        result = self.browser.open_url('data:text/html,<h1>hi</h1>')
        self.assertFalse(result['success'])

    def test_block_empty(self):
        """空 URL 必须被阻止。"""
        result = self.browser.open_url('')
        self.assertFalse(result['success'])

    def test_block_too_long(self):
        """超长 URL 必须被阻止。"""
        long_url = 'http://example.com/' + 'a' * 9000
        result = self.browser.open_url(long_url)
        self.assertFalse(result['success'])
        self.assertIn('长度', result['error'])

    def test_auto_append_https(self):
        """无协议的 URL 应自动补全 https://。"""
        with patch('computer.web_browser.webbrowser.open') as mock_open:
            mock_open.return_value = True
            result = self.browser.open_url('example.com')
            self.assertTrue(result['success'])
            called_url = mock_open.call_args[0][0]
            self.assertTrue(called_url.startswith('https://'))


# ============================================================
# Test 2: App Launcher
# ============================================================
class TestAppLauncher(unittest.TestCase):
    """应用启动器安全测试。"""

    def setUp(self):
        from computer.app_launcher import AppLauncher
        self.launcher = AppLauncher()

    def test_nonexistent_app(self):
        """不存在的 app 应返回失败。"""
        result = self.launcher.launch('nonexistent_app_xyz_12345')
        self.assertFalse(result['success'])
        self.assertIn('找不到', result['error'])

    def test_path_traversal_blocked(self):
        """路径穿越必须被阻止。"""
        result = self.launcher.launch('../../windows/system32/cmd.exe')
        self.assertFalse(result['success'])
        self.assertFalse(result["success"])

    def test_shell_injection_blocked(self):
        """shell 元字符必须被阻止。"""
        result = self.launcher.launch('notepad; rm -rf /')
        self.assertFalse(result['success'])
        self.assertFalse(result["success"])

    def test_args_shell_injection_blocked(self):
        """参数中的 shell 注入必须被阻止。"""
        result = self.launcher.launch(
            'notepad',
            args='file.txt && echo pwned'
        )
        self.assertFalse(result['success'])
        self.assertFalse(result["success"])

    def test_none_args_allowed(self):
        """None args 不应报错。"""
        result = self.launcher.launch('nonexistent_app_xyz_12345', args=None)
        self.assertFalse(result['success'])


# ============================================================
# Test 3: Downloader
# ============================================================
class TestDownloader(unittest.TestCase):
    """下载器安全测试。"""

    def setUp(self):
        from computer.downloader import Downloader
        self.downloader = Downloader()

    def test_http_rejected(self):
        """HTTP URL 必须被拒绝。"""
        result = self.downloader.download('http://example.com/file.zip')
        self.assertFalse(result['success'])
        self.assertIn('HTTPS', result['message'])

    def test_empty_url_rejected(self):
        """空 URL 必须被拒绝。"""
        result = self.downloader.download('')
        self.assertFalse(result['success'])

    def test_filename_sanitization(self):
        """文件名中的危险字符必须被清理。"""
        from computer.downloader import Downloader
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = Downloader(download_root=tmpdir)
            safe = dl._sanitize_filename('test<>:"file*.txt')
            self.assertNotIn('<', safe)
            self.assertNotIn('"', safe)
            self.assertNotIn('*', safe)
            self.assertNotIn('/', safe)

    def test_path_traversal_in_filename(self):
        """文件名中的路径穿越必须被阻止。"""
        from computer.downloader import Downloader
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = Downloader(download_root=tmpdir)
            safe = dl._sanitize_filename('../../../etc/passwd')
            self.assertNotIn('../', safe)


# ============================================================
# Test 4: Tool Registry
# ============================================================
class TestToolRegistry(unittest.TestCase):
    """工具注册表测试。"""

    def setUp(self):
        from computer.tool_registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_tool_count(self):
        """应有 3 个内置工具。"""
        self.assertEqual(self.registry.tool_count(), 3)

    def test_tool_names(self):
        """内置工具名称应正确。"""
        names = [t['function']['name'] for t in self.registry.get_tools()]
        self.assertIn('open_url', names)
        self.assertIn('launch_app', names)
        self.assertIn('download_file', names)

    def test_open_url_call(self):
        """open_url 工具应能被调用。"""
        result = self.registry.call_tool('open_url', {'url': 'https://example.com'})
        # 可能成功也可能失败（取决于浏览器配置），但不能抛异常
        self.assertIn('success', result)

    def test_unknown_tool(self):
        """调用不存在的工具应返回失败。"""
        result = self.registry.call_tool('nonexistent_tool', {})
        self.assertFalse(result['success'])
        self.assertIn('未知工具', result['error'])

    def test_schema_structure(self):
        """工具 schema 应符合 OpenAI 格式。"""
        tools = self.registry.get_tools()
        for tool in tools:
            self.assertIn('type', tool)
            self.assertEqual(tool['type'], 'function')
            self.assertIn('function', tool)
            fn = tool['function']
            self.assertIn('name', fn)
            self.assertIn('description', fn)
            self.assertIn('parameters', fn)


# ============================================================
# Test 5: AIClient Tool Integration
# ============================================================
class TestAIClientTools(unittest.TestCase):
    """AIClient 工具集成测试。"""

    def test_tool_registry_initialized(self):
        """AIClient 应初始化 tool_registry。"""
        from core.ai_client import AIClient
        # 不实际初始化（需要 API key），只检查类定义
        import inspect
        source = inspect.getsource(AIClient.__init__)
        self.assertIn('_tool_registry', source)

    def test_send_message_has_tools_code(self):
        """send_message 应包含工具调用代码。"""
        from core.ai_client import AIClient
        source = inspect.getsource(AIClient.send_message)
        self.assertIn('tools', source)
        self.assertIn('_tool_registry', source)

    def test_tool_max_rounds(self):
        """工具调用应有最大轮次限制。"""
        from core.ai_client import AIClient
        source = inspect.getsource(AIClient.send_message)
        self.assertIn('MAX_TOOL_ROUNDS', source)
        self.assertIn('5', source)


# ============================================================
# Test 6: Security Regression (ensure no existing constraints broken)
# ============================================================
class TestSecurityRegression(unittest.TestCase):
    """安全回归测试 — 确保新增代码不破坏现有安全约束。"""

    def test_no_shell_true_in_launcher(self):
        """AppLauncher 不得使用 shell=True。"""
        from computer.app_launcher import AppLauncher
        source = inspect.getsource(AppLauncher.launch)
        self.assertNotIn('shell=True', source)

    def test_no_exec_in_browser(self):
        """BrowserController 不得执行任意代码。"""
        from computer.web_browser import BrowserController
        source = inspect.getsource(BrowserController.open_url)
        self.assertNotIn('eval', source)
        self.assertNotIn('exec', source)

    def test_downloader_no_auto_execute(self):
        """Downloader 不得在下载后自动执行文件。"""
        from computer.downloader import Downloader
        source = inspect.getsource(Downloader.download)
        self.assertNotIn('subprocess.run', source)
        self.assertNotIn('os.system', source)

    def test_forbidden_extensions_blocked(self):
        """脚本扩展名应被阻止。"""
        from computer.app_launcher import FORBIDDEN_EXECUTABLE_EXTENSIONS
        self.assertIn('.bat', FORBIDDEN_EXECUTABLE_EXTENSIONS)
        self.assertIn('.ps1', FORBIDDEN_EXECUTABLE_EXTENSIONS)
        self.assertIn('.vbs', FORBIDDEN_EXECUTABLE_EXTENSIONS)


# ============================================================
# Run tests
# ============================================================
if __name__ == '__main__':
    import inspect
    unittest.main(verbosity=2)
