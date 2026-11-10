
"""AronaClient V0.2 — 真实 Windows 桌面会话截图验收测试。

与 test_qt_capture.py 的区别（重要）：
    test_qt_capture.py 强制使用 offscreen 虚拟平台，只证明「截图逻辑
    能生成 PNG」，**不能**代表真实桌面可用。
    本文件不设置 QT_QPA_PLATFORM，使用系统默认平台插件；只有在
    平台插件确认为真实桌面（windows/xcb/cocoa）且存在显示器时才运行，
    否则整体跳过并给出明确原因。

隐私保护：
    * 截图只写入临时目录，断言后立即删除。
    * 不打印、不保存、不上传截图内容。
    * 不把截图附加到任何报告或提交。

安全：
    * 不修改项目的截图授权策略；测试通过 desktop_access 正常注册授权。
    * 不伪造显示器数量或替换返回数据。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 关键：如果外部把平台强制成 offscreen，本文件的真实桌面验收无意义，
# 必须跳过而不是把 offscreen 结果当成真实桌面结果。
_FORCED_OFFSCREEN = (
    __import__("os").environ.get("QT_QPA_PLATFORM", "").strip().lower()
    == "offscreen"
)

REAL_DESKTOP_PLATFORMS = {"windows", "xcb", "cocoa", "wayland"}

QT_APP = None
SKIP_REASON = ""

if _FORCED_OFFSCREEN:
    SKIP_REASON = "QT_QPA_PLATFORM=offscreen，无法验证真实桌面"
else:
    try:
        from PySide6.QtWidgets import QApplication

        QT_APP = QApplication.instance() or QApplication([])
        _platform = QT_APP.platformName()
        if _platform not in REAL_DESKTOP_PLATFORMS:
            SKIP_REASON = (
                f"当前 Qt 平台为 {_platform!r}，不是真实桌面平台"
            )
            QT_APP = None
        elif not QT_APP.screens():
            SKIP_REASON = "当前环境没有可用显示器"
            QT_APP = None
    except Exception as exc:  # pragma: no cover
        SKIP_REASON = f"无法初始化 Qt 应用：{type(exc).__name__}: {exc}"


import computer.desktop_access as da


@unittest.skipIf(QT_APP is None, f"跳过真实桌面验收：{SKIP_REASON}")
class TestRealDesktopCapture(unittest.TestCase):
    """真实 Windows 桌面会话下的截图验收。"""

    @classmethod
    def setUpClass(cls):
        from computer.screen_capture import ScreenCapture

        cls.tmp = Path(tempfile.mkdtemp(prefix="arona_realtest_capture_"))
        cls.capture = ScreenCapture(output_dir=cls.tmp)
        cls.platform_name = QT_APP.platformName()
        cls.screens = list(QT_APP.screens())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        da.set_provider(
            lambda: (True, "真实桌面验收授权"), allow_replace=True
        )

    def tearDown(self):
        # 清理本用例可能产生的截图
        for p in self.tmp.glob("*.png"):
            try:
                p.unlink()
            except OSError:
                pass

    def test_platform_is_real_desktop(self):
        """确认运行在真实桌面平台而非 offscreen。"""
        self.assertIn(self.platform_name, REAL_DESKTOP_PLATFORMS)
        self.assertNotEqual(self.platform_name, "offscreen")
        self.assertGreater(len(self.screens), 0)

    def test_capture_success_on_real_desktop(self):
        """真实桌面截图成功并生成有效 PNG。"""
        from PySide6.QtGui import QImage

        result = self.capture.capture_screen()
        self.assertTrue(result["success"], result.get("error"))

        path = Path(result["path"])
        self.assertTrue(path.exists(), "截图文件必须存在")
        self.assertGreater(path.stat().st_size, 100, "PNG 文件大小应合理")

        data = path.read_bytes()
        self.assertEqual(
            data[:8], b"\x89PNG\r\n\x1a\n", "PNG 文件头必须有效"
        )

        image = QImage(str(path))
        self.assertFalse(image.isNull(), "QImage 必须能重新读取")

        # 报告尺寸与实际图像尺寸一致
        self.assertEqual(image.width(), result["width"])
        self.assertEqual(image.height(), result["height"])

        # 与真实屏幕几何一致
        primary = QT_APP.primaryScreen()
        geometry = primary.geometry()
        self.assertEqual(result["width"], geometry.width())
        self.assertEqual(result["height"], geometry.height())

        self.assertGreater(result["device_pixel_ratio"], 0)
        self.assertIsNone(result["error"])

    def test_capture_specific_screen_index(self):
        """指定显示器索引 0 可截图。"""
        result = self.capture.capture_screen(screen_index=0)
        self.assertTrue(result["success"], result.get("error"))
        self.assertGreater(result["width"], 0)
        self.assertGreater(result["height"], 0)

    def test_index_out_of_range_rejected(self):
        """索引越界被拒绝。"""
        result = self.capture.capture_screen(screen_index=999)
        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertIn("IndexError", result["error"])

    def test_index_wrong_type_rejected(self):
        """索引类型错误被拒绝。"""
        result = self.capture.capture_screen(screen_index="0")
        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertIn("TypeError", result["error"])

    def test_unauthorized_writes_nothing(self):
        """未授权时不截图、不写盘。"""
        da.clear_provider()
        before = set(self.tmp.glob("*.png"))
        result = self.capture.capture_screen()
        after = set(self.tmp.glob("*.png"))

        self.assertFalse(result["success"])
        self.assertIsNone(result["path"])
        self.assertEqual(before, after, "未授权时不得写盘")

    def test_screen_info_matches_qt(self):
        """get_screen_info 与 Qt 报告的显示器一致。"""
        info = self.capture.get_screen_info()
        self.assertEqual(len(info), len(self.screens))

        primary_geo = QT_APP.primaryScreen().geometry()
        primary_info = next((i for i in info if i["primary"]), None)
        self.assertIsNotNone(primary_info)
        self.assertEqual(primary_info["geometry"]["width"], primary_geo.width())
        self.assertEqual(primary_info["geometry"]["height"], primary_geo.height())

    def test_path_traversal_contained(self):
        """文件名不能逃逸出截图目录。"""
        result = self.capture.capture_screen(filename="../../escape")
        self.assertTrue(result["success"], result.get("error"))

        resolved = Path(result["path"]).resolve()
        self.assertEqual(
            resolved.parent,
            self.tmp.resolve(),
            "截图必须保存在指定目录内",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
