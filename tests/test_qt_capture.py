
"""AronaClient V0.2 — Qt 截图成功路径测试（offscreen 平台）。

覆盖此前未验证的截图成功路径：
    * 真实创建 QApplication 实例
    * 真实调用 screen.grabWindow(0) 得到有效图像
    * 真实写盘并验证 PNG 格式、尺寸、可回读
    * 自定义文件名、自动文件名、路径穿越防护
    * 索引越界、类型错误、未授权、无 Qt 实例

安全说明：
    * 使用 offscreen 平台插件，截图内容是虚拟屏幕（非用户真实桌面），
      不会读取任何用户私人内容。
    * 不把截图上传到任何外部服务。
    * 测试产生的 PNG 在断言后立即删除。
    * 若环境无法创建 QApplication，整套测试自动跳过并标记原因。
"""

import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 必须在导入 PySide6 之前设置，确保无真实桌面时也能运行
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QT_APP = None
QT_SKIP_REASON = ""

try:
    from PySide6.QtWidgets import QApplication

    QT_APP = QApplication.instance() or QApplication([])
    _SCREENS = QT_APP.screens()
    if not _SCREENS:
        QT_SKIP_REASON = "当前环境没有可用显示器"
except Exception as exc:  # pragma: no cover
    QT_SKIP_REASON = f"无法初始化 Qt 应用：{type(exc).__name__}: {exc}"


import computer.desktop_access as da


def setUpModule():
    """模块级：注册测试授权。"""
    da.set_provider(lambda: (True, "测试授权"), allow_replace=True)


def tearDownModule():
    da.clear_provider()


@unittest.skipIf(QT_APP is None, f"Qt 不可用：{QT_SKIP_REASON}")
class TestQtCaptureSuccess(unittest.TestCase):
    """Qt 截图成功路径。"""

    @classmethod
    def setUpClass(cls):
        from computer.screen_capture import ScreenCapture

        cls.capture = ScreenCapture()
        cls.created = []

    @classmethod
    def tearDownClass(cls):
        for p in cls.created:
            try:
                Path(p).unlink()
            except OSError:
                pass

    def _track(self, result):
        if result.get("success") and result.get("path"):
            self.created.append(result["path"])
        return result

    def test_capture_success_returns_valid_png(self):
        """截图成功并生成有效 PNG。"""
        result = self._track(self.capture.capture_screen())

        self.assertTrue(result["success"], result.get("error"))
        self.assertIsNotNone(result["path"])
        self.assertIsNone(result["error"])

        path = Path(result["path"])
        self.assertTrue(path.exists())

        data = path.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

        self.assertGreater(result["width"], 0)
        self.assertGreater(result["height"], 0)
        self.assertGreater(result["device_pixel_ratio"], 0)

    def test_captured_image_is_readable(self):
        """截图可被重新读入为有效图像。"""
        from PySide6.QtGui import QImage

        result = self._track(self.capture.capture_screen())
        self.assertTrue(result["success"], result.get("error"))

        image = QImage(result["path"])
        self.assertFalse(image.isNull())
        self.assertEqual(image.width(), result["width"])
        self.assertEqual(image.height(), result["height"])

    def test_capture_specific_screen_index(self):
        """指定有效显示器索引可截图。"""
        result = self._track(self.capture.capture_screen(screen_index=0))
        self.assertTrue(result["success"], result.get("error"))
        self.assertGreater(result["width"], 0)

    def test_capture_custom_filename(self):
        """自定义文件名自动补 .png 后缀。"""
        result = self._track(
            self.capture.capture_screen(filename="arona_test_capture")
        )
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(result["path"].endswith(".png"))
        self.assertTrue(Path(result["path"]).exists())

    def test_capture_path_traversal_blocked(self):
        """文件名不能逃逸到 captures 目录之外。"""
        result = self._track(
            self.capture.capture_screen(filename="../../evil")
        )
        self.assertTrue(result["success"], result.get("error"))

        path = Path(result["path"]).resolve()
        expected_root = self.capture.output_dir.resolve()
        self.assertEqual(
            path.parent, expected_root,
            "截图必须保存在 captures 目录内",
        )

    def test_screen_info_structure(self):
        """get_screen_info 返回结构完整的列表。"""
        info = self.capture.get_screen_info()
        self.assertIsInstance(info, list)
        self.assertGreater(len(info), 0)

        for item in info:
            for key in (
                "index", "name", "geometry",
                "available_geometry", "device_pixel_ratio", "primary",
            ):
                self.assertIn(key, item)
            self.assertIn("width", item["geometry"])
            self.assertIn("height", item["geometry"])


@unittest.skipIf(QT_APP is None, f"Qt 不可用：{QT_SKIP_REASON}")
class TestQtCaptureFailures(unittest.TestCase):
    """截图失败路径。"""

    @classmethod
    def setUpClass(cls):
        from computer.screen_capture import ScreenCapture

        cls.capture = ScreenCapture()

    def test_index_out_of_range(self):
        """显示器索引越界返回失败。"""
        result = self.capture.capture_screen(screen_index=999)
        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertIn("IndexError", result["error"])

    def test_index_wrong_type(self):
        """索引类型错误返回失败。"""
        result = self.capture.capture_screen(screen_index="0")
        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertIn("TypeError", result["error"])

    def test_unauthorized_no_file_written(self):
        """未授权时不截图、不写盘。"""
        da.clear_provider()
        try:
            before = set(self.capture.output_dir.glob("*.png"))
            result = self.capture.capture_screen()
            after = set(self.capture.output_dir.glob("*.png"))
        finally:
            da.set_provider(lambda: (True, "测试授权"), allow_replace=True)

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertEqual(before, after, "未授权时不得写盘")

    def test_unauthorized_screen_info_empty(self):
        """未授权时 get_screen_info 返回空列表。"""
        da.clear_provider()
        try:
            info = self.capture.get_screen_info()
        finally:
            da.set_provider(lambda: (True, "测试授权"), allow_replace=True)

        self.assertEqual(info, [])


class TestCaptureWithoutQtApp(unittest.TestCase):
    """无 Qt 应用实例时的行为（不依赖真实 Qt）。"""

    def test_capture_fails_cleanly_without_app(self):
        """没有 QApplication 时返回失败而非抛异常。"""
        from unittest.mock import patch

        from computer.screen_capture import ScreenCapture

        capture = ScreenCapture()
        with patch(
            "computer.screen_capture.QGuiApplication.instance",
            return_value=None,
        ):
            result = capture.capture_screen()

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertIsNotNone(result["error"])

    def test_screen_info_raises_without_app(self):
        """无 QApplication 时 get_screen_info 抛出明确异常。"""
        from unittest.mock import patch

        from computer.screen_capture import ScreenCapture

        capture = ScreenCapture()
        da.set_provider(lambda: (True, "测试授权"), allow_replace=True)
        try:
            with patch(
                "computer.screen_capture.QGuiApplication.instance",
                return_value=None,
            ):
                with self.assertRaises(RuntimeError):
                    capture.get_screen_info()
        finally:
            # 必须恢复授权，否则会影响后续测试类（unittest 按类名字母序执行）
            da.set_provider(
                lambda: (True, "测试授权"), allow_replace=True
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
