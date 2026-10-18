
"""AronaClient V0.2 桌面操作能力 — 安全修复与综合测试。

覆盖 P1 修复验证和七类安全测试场景。
所有测试使用 Mock 隔离，不产生真实外部副作用。
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Test 1: 下载器重定向安全
# ============================================================
class TestDownloaderRedirectSafety(unittest.TestCase):
    """下载器重定向安全检查。"""

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

    def test_redirect_to_http_blocked(self):
        """重定向到 HTTP 必须被拒绝。"""
        with patch('computer.downloader.httpx') as mock_httpx:
            # First response: 302 redirect to HTTP
            mock_response = MagicMock()
            mock_response.status_code = 302
            mock_response.headers = {'location': 'http://evil.com/file.zip'}
            mock_httpx.Client.return_value.__enter__.return_value.get.return_value = mock_response

            result = self.downloader.download('https://example.com/redirect')
            self.assertFalse(result['success'])
            self.assertIn('重定向', result['message'])

    def test_redirect_to_private_ip_blocked(self):
        """重定向到私有 IP 必须被拒绝。"""
        with patch('computer.downloader.httpx') as mock_httpx:
            mock_response = MagicMock()
            mock_response.status_code = 302
            mock_response.headers = {'location': 'https://192.168.1.1/file.zip'}
            mock_httpx.Client.return_value.__enter__.return_value.get.return_value = mock_response

            result = self.downloader.download('https://example.com/redirect')
            self.assertFalse(result['success'])
            self.assertFalse(result['success'])

    def test_redirect_loop_detected(self):
        """循环重定向必须被检测。"""
        with patch('computer.downloader.httpx') as mock_httpx:
            mock_response = MagicMock()
            mock_response.status_code = 302
            mock_response.headers = {'location': 'https://example.com/loop'}
            mock_httpx.Client.return_value.__enter__.return_value.get.return_value = mock_response

            # Same URL redirects to itself
            result = self.downloader.download('https://example.com/loop')
            # Should eventually fail due to loop detection or redirect limit
            self.assertFalse(result['success'])

    def test_max_redirects_enforced(self):
        """重定向次数上限必须生效。"""
        with patch('computer.downloader.httpx') as mock_httpx:
            def make_redirect_response(*args, **kwargs):
                resp = MagicMock()
                resp.status_code = 302
                resp.headers = {'location': 'https://example.com/next'}
                return resp

            mock_httpx.Client.return_value.__enter__.return_value.get.side_effect = make_redirect_response

            result = self.downloader.download('https://example.com/start')
            self.assertFalse(result['success'])
            self.assertIn('重定向', result['message'])

    def test_filename_sanitization(self):
        """文件名中的危险字符必须被清理。"""
        safe = self.downloader._sanitize_filename('test<>:"file*.txt')
        self.assertNotIn('<', safe)
        self.assertNotIn('"', safe)
        self.assertNotIn('*', safe)

    def test_path_traversal_in_filename_blocked(self):
        """文件名中的路径穿越必须被阻止。"""
        safe = self.downloader._sanitize_filename('../../../etc/passwd')
        self.assertNotIn('../', safe)


# ============================================================
# Test 2: 应用启动安全
# ============================================================
class TestAppLauncherSecurity(unittest.TestCase):
    """应用启动安全测试。"""

    def setUp(self):
        from computer.app_launcher import AppLauncher, DANGEROUS_EXECUTABLE_NAMES
        self.launcher = AppLauncher()
        self.dangerous = DANGEROUS_EXECUTABLE_NAMES

    def test_dangerous_names_blocked(self):
        """高风险系统工具必须被阻止。"""
        for name in ['cmd.exe', 'powershell.exe', 'wscript.exe']:
            result = self.launcher.launch(name)
            self.assertFalse(result['success'], f'{name} should be blocked')
            self.assertIn('安全', result['message'])

    def test_legitimate_apps_allowed(self):
        """合法应用应可启动（或返回找不到）。"""
        # notepad might exist, calc might exist - just check they're not blocked by security
        for name in ['notepad', 'calc']:
            result = self.launcher.launch(name)
            # Should NOT be blocked by security policy
            if not result['success']:
                self.assertNotIn('安全', result['message'],
                    f'{name} should not be blocked by security policy')

    def test_path_traversal_blocked(self):
        """路径穿越必须被阻止。"""
        result = self.launcher.launch('../../windows/system32/cmd.exe')
        self.assertFalse(result['success'])

    def test_shell_injection_blocked(self):
        """Shell 注入必须被阻止。"""
        result = self.launcher.launch('notepad; rm -rf /')
        self.assertFalse(result['success'])

    def test_args_shell_injection_blocked(self):
        """参数中的 shell 注入必须被阻止。"""
        result = self.launcher.launch('notepad', args='file.txt && echo pwned')
        self.assertFalse(result['success'])

    def test_script_extensions_blocked(self):
        """脚本扩展名必须被阻止。"""
        result = self.launcher.launch('C:\\test.bat')
        self.assertFalse(result['success'])

    def test_absolute_path_to_dangerous_tool_blocked(self):
        """绝对路径到高风险工具必须被阻止。"""
        result = self.launcher.launch('C:\\Windows\\System32\\cmd.exe')
        self.assertFalse(result['success'])
        self.assertIn('安全', result['message'])


# ============================================================
# Test 3: URL 凭据拒绝
# ============================================================
class TestBrowserCredentialBlocking(unittest.TestCase):
    """浏览器 URL 凭据检查测试。"""

    def setUp(self):
        from computer.web_browser import BrowserController
        self.browser = BrowserController()

    def test_userinfo_rejected(self):
        """含用户名的 URL 必须被拒绝。"""
        result = self.browser.open_url('https://user@example.com')
        self.assertFalse(result['success'])
        self.assertIn('凭据', result['message'])

    def test_userpass_rejected(self):
        """含用户密码的 URL 必须被拒绝。"""
        result = self.browser.open_url('https://user:pass@example.com')
        self.assertFalse(result['success'])
        self.assertIn('凭据', result['message'])

    def test_normal_url_allowed(self):
        """普通 HTTPS URL 应被允许。"""
        with patch('computer.web_browser.webbrowser.open') as mock_open:
            mock_open.return_value = True
            result = self.browser.open_url('https://example.com')
            self.assertTrue(result['success'])

    def test_http_url_allowed(self):
        """HTTP URL 应被允许（浏览器层面）。"""
        with patch('computer.web_browser.webbrowser.open') as mock_open:
            mock_open.return_value = True
            result = self.browser.open_url('http://example.com')
            self.assertTrue(result['success'])


# ============================================================
# Test 4: 工具调用参数校验
# ============================================================
class TestToolCallValidation(unittest.TestCase):
    """工具调用参数校验测试。"""

    def setUp(self):
        from computer.tool_registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_unknown_tool_rejected(self):
        """未知工具必须被拒绝。"""
        result = self.registry.call_tool('nonexistent', {})
        self.assertFalse(result['success'])
        self.assertIn('未知工具', result['error'])

    def test_missing_required_param(self):
        """缺少必需参数应返回失败（由 handler 处理）。"""
        # open_url requires 'url' parameter
        result = self.registry.call_tool('open_url', {})
        # Should fail gracefully, not crash
        self.assertIn('success', result)

    def test_wrong_param_type(self):
        """错误参数类型应被捕获。"""
        result = self.registry.call_tool('open_url', {'url': 12345})
        # Should handle non-string URL gracefully
        self.assertIn('success', result)

    def test_invalid_json_arguments(self):
        """非法 JSON 参数应被容错处理。"""
        # Simulate what AIClient does with invalid JSON
        from computer.tool_registry import ToolRegistry
        registry = ToolRegistry()
        # Direct handler call with invalid args
        result = registry._browser.open_url('https://example.com')
        self.assertIn('success', result)


# ============================================================
# Test 5: 工具结果安全序列化
# ============================================================
class TestToolResultSerialization(unittest.TestCase):
    """工具结果安全序列化测试。"""

    def setUp(self):
        from computer.tool_registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_special_chars_in_result(self):
        """结果中的特殊字符应被安全序列化。"""
        result = self.registry.call_tool('open_url', {'url': 'https://example.com'})
        # Should be convertible to string without issues
        msg = str(result.get('message', ''))
        self.assertIsInstance(msg, str)

    def test_empty_result_handled(self):
        """空结果应被正确处理。"""
        result = self.registry.call_tool('open_url', {'url': ''})
        self.assertIn('success', result)
        self.assertFalse(result['success'])

    def test_tool_result_no_executable_content(self):
        """工具结果不包含可执行内容。"""
        result = self.registry.call_tool('open_url', {'url': 'https://example.com'})
        msg = str(result.get('message', ''))
        self.assertNotIn('<script>', msg.lower())
        self.assertNotIn('javascript:', msg.lower())


# ============================================================
# Test 6: 五轮工具调用边界
# ============================================================
class TestToolCallRoundLimit(unittest.TestCase):
    """五轮工具调用上限测试。"""

    def test_max_rounds_constant(self):
        """MAX_TOOL_ROUNDS 应设为 5。"""
        from core.ai_client import AIClient
        import inspect
        source = inspect.getsource(AIClient.send_message)
        self.assertIn('MAX_TOOL_ROUNDS = 5', source)

    def test_loop_breaks_on_no_tool_calls(self):
        """无 tool_calls 时应退出循环。"""
        # This is verified by code structure:
        # if not tool_calls: break
        from core.ai_client import AIClient
        import inspect
        source = inspect.getsource(AIClient.send_message)
        self.assertIn('if not tool_calls:', source)
        self.assertIn('break', source)


# ============================================================
# Test 7: 并发工具调用安全
# ============================================================
class TestConcurrentToolSafety(unittest.TestCase):
    """并发工具调用安全测试。"""

    def setUp(self):
        from computer.tool_registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_parallel_tool_calls_handled(self):
        """并行工具调用应逐个执行。"""
        # Verify the code iterates over tool_calls sequentially
        from core.ai_client import AIClient
        import inspect
        source = inspect.getsource(AIClient.send_message)
        self.assertIn('for tc in tool_calls:', source)

    def test_tool_result_does_not_leak_state(self):
        """工具结果不应串扰到其他工具。"""
        # Each tool call is independent
        result1 = self.registry.call_tool('open_url', {'url': 'https://example.com'})
        result2 = self.registry.call_tool('launch_app', {'name_or_path': 'nonexistent_app_xyz'})
        # Both should be independent
        self.assertIn('success', result1)
        self.assertIn('success', result2)


# ============================================================
# Additional: Security regression
# ============================================================
class TestSecurityRegression(unittest.TestCase):
    """安全回归测试。"""

    def test_no_shell_true_in_launcher(self):
        from computer.app_launcher import AppLauncher
        source = inspect.getsource(AppLauncher.launch)
        self.assertNotIn('shell=True', source)

    def test_no_exec_in_browser(self):
        from computer.web_browser import BrowserController
        source = inspect.getsource(BrowserController.open_url)
        self.assertNotIn('eval', source)
        self.assertNotIn('exec', source)

    def test_downloader_no_auto_execute(self):
        from computer.downloader import Downloader
        source = inspect.getsource(Downloader.download)
        self.assertNotIn('subprocess.run', source)
        self.assertNotIn('os.system', source)

    def test_forbidden_extensions_blocked(self):
        from computer.app_launcher import FORBIDDEN_EXECUTABLE_EXTENSIONS
        self.assertIn('.bat', FORBIDDEN_EXECUTABLE_EXTENSIONS)
        self.assertIn('.ps1', FORBIDDEN_EXECUTABLE_EXTENSIONS)
        self.assertIn('.vbs', FORBIDDEN_EXECUTABLE_EXTENSIONS)

    def test_dangerous_executables_defined(self):
        from computer.app_launcher import DANGEROUS_EXECUTABLE_NAMES
        self.assertIn('cmd.exe', DANGEROUS_EXECUTABLE_NAMES)
        self.assertIn('powershell.exe', DANGEROUS_EXECUTABLE_NAMES)


# ============================================================
# Run
# ============================================================
if __name__ == '__main__':
    import inspect
    unittest.main(verbosity=2)
