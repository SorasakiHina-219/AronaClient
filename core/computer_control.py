# core/computer_control.py
# AronaClient - 本地电脑控制模块
#
# 功能：
# 1. 获取屏幕尺寸
# 2. 截取当前桌面
# 3. 移动鼠标
# 4. 鼠标点击 / 双击 / 右键
# 5. 输入文字
# 6. 按键 / 组合键
#
# 安全：
# - pyautogui.FAILSAFE = True
# - 鼠标移动到屏幕左上角可以触发 PyAutoGUI 的紧急停止
# - 不包含删除文件、执行 Shell 命令等危险操作

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pyautogui


class ComputerControlError(Exception):
    """电脑控制相关的错误基类。"""


class ComputerControlStopped(ComputerControlError):
    """控制器已被停止，拒绝继续执行任何操作。"""


class ComputerController:
    """
    AronaClient 本地电脑控制器。

    这个类负责真正操作用户自己的电脑。
    AI 层以后只需要调用这里提供的方法即可。
    """

    def __init__(
        self,
        pause: float = 0.15,
    ):
        # PyAutoGUI 安全机制：
        # 鼠标移动到屏幕左上角时，会触发 FailSafeException。
        pyautogui.FAILSAFE = True

        # 每次 PyAutoGUI 操作之间的默认等待时间。
        pyautogui.PAUSE = pause

        self.pause = pause

        # ----------------------------------------------------
        # 停止机制
        # ----------------------------------------------------
        #
        # _stopped        : 本地硬停止标记，一旦置位只有 reset_stop() 能解除
        # _stop_check     : 外部（任务层）提供的停止判据，每次操作前查询
        self._stopped = False
        self._stop_check = None

        # 授权判据。
        #
        # None 表示「未注册」——此时 _check_authorization() 会拒绝执行，
        # 也就是默认拒绝，而不是默认放行。
        self._auth_check = None

        # 当前被按下的鼠标键。
        #
        # 有了它，无论操作是成功、失败还是被停止，
        # release_all() 都能把按键释放掉，不会让鼠标卡在按下状态。
        self._pressed = set()

    # ============================================================
    # 屏幕
    # ============================================================

    def get_screen_size(self) -> tuple[int, int]:
        """
        获取主显示器分辨率。

        返回：
            (width, height)
        """

        self._check_stop()

        self._check_authorization()

        width, height = pyautogui.size()

        return int(width), int(height)

    def capture_screen(
        self,
        save_path: Optional[str] = None,
    ) -> str:
        """
        截取当前桌面。

        参数：
            save_path:
                指定保存位置。
                如果不指定，则自动保存到 AronaClient/screenshots。

        返回：
            截图文件的完整路径。
        """

        if save_path is None:
            project_root = Path(__file__).resolve().parent.parent

            screenshot_dir = (
                project_root / "screenshots"
            )

            screenshot_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            timestamp = time.strftime(
                "%Y%m%d_%H%M%S"
            )

            save_path = (
                screenshot_dir
                / f"desktop_{timestamp}.png"
            )

        else:
            save_path = Path(save_path)

            save_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

        self._check_stop()

        self._check_authorization()

        screenshot = pyautogui.screenshot()

        screenshot.save(
            str(save_path)
        )

        return str(
            Path(save_path).resolve()
        )

    # ============================================================
    # 鼠标
    # ============================================================

    def get_mouse_position(self) -> tuple[int, int]:
        """
        获取当前鼠标坐标。
        """

        self._check_stop()

        self._check_authorization()

        x, y = pyautogui.position()

        return int(x), int(y)

    def move_mouse(
        self,
        x: int,
        y: int,
        duration: float = 0.2,
    ) -> None:
        """
        移动鼠标到指定坐标。
        """

        self._check_stop()

        self._check_authorization()

        pyautogui.moveTo(
            int(x),
            int(y),
            duration=duration,
        )

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
    ) -> None:
        """
        鼠标点击。

        如果不提供 x/y：
            点击当前位置。

        button：
            left
            right
            middle
        """

        self._check_stop()

        self._check_authorization()

        if x is not None and y is not None:
            pyautogui.click(
                x=int(x),
                y=int(y),
                clicks=int(clicks),
                button=button,
            )
        else:
            pyautogui.click(
                clicks=int(clicks),
                button=button,
            )

    def double_click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> None:
        """
        双击鼠标左键。
        """

        self.click(
            x=x,
            y=y,
            button="left",
            clicks=2,
        )

    def right_click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> None:
        """
        鼠标右键点击。
        """

        self.click(
            x=x,
            y=y,
            button="right",
            clicks=1,
        )

    def mouse_down(
        self,
        button: str = "left",
    ) -> None:
        """
        按下鼠标按键。
        """

        self._check_stop()

        self._check_authorization()

        pyautogui.mouseDown(
            button=button
        )

        self._pressed.add(str(button))

    def mouse_up(
        self,
        button: str = "left",
    ) -> None:
        """
        松开鼠标按键。
        """

        self._check_stop()

        self._check_authorization()

        pyautogui.mouseUp(
            button=button
        )

        self._pressed.discard(str(button))

    # ============================================================
    # 键盘
    # ============================================================

    def type_text(
        self,
        text: str,
        interval: float = 0.02,
    ) -> None:
        """
        输入文字。

        注意：
        PyAutoGUI 对中文直接输入支持有限。
        英文、数字、常见符号可以直接输入。

        中文输入法控制以后单独处理。
        """

        if not text:
            return

        self._check_stop()

        self._check_authorization()

        pyautogui.write(
            str(text),
            interval=interval,
        )

    def press_key(
        self,
        key: str,
        presses: int = 1,
        interval: float = 0.05,
    ) -> None:
        """
        按键。

        例如：

            press_key("enter")
            press_key("esc")
            press_key("space")
            press_key("tab")
        """

        self._check_stop()

        self._check_authorization()

        pyautogui.press(
            key,
            presses=int(presses),
            interval=interval,
        )

    def hotkey(
        self,
        *keys: str,
    ) -> None:
        """
        按下组合键。

        例如：

            hotkey("ctrl", "c")
            hotkey("ctrl", "v")
            hotkey("alt", "tab")
            hotkey("ctrl", "shift", "esc")
        """

        if not keys:
            return

        self._check_stop()

        self._check_authorization()

        pyautogui.hotkey(
            *keys
        )

    # ============================================================
    # 滚轮
    # ============================================================

    def scroll(
        self,
        amount: int,
    ) -> None:
        """
        滚动鼠标滚轮。

        正数：
            向上

        负数：
            向下
        """

        self._check_stop()

        self._check_authorization()

        pyautogui.scroll(
            int(amount)
        )

    # ============================================================
    # 安全停止
    # ============================================================

    def set_stop_check(self, fn) -> None:
        """
        注册一个「是否已停止」的判据。

        任务层把它自己的停止信号接进来，
        这样在执行过程中只要外部发出停止，控制器就会在下一个操作前拒绝执行。
        """

        self._stop_check = fn

    def _check_stop(self) -> None:
        """
        每个操作执行前都必须先过这里。

        本地硬停止与外部停止信号任一命中都会抛异常，
        保证停止之后不会再有任何实际操作发出去。
        """

        if self._stopped:

            raise ComputerControlStopped(
                "控制器已处于停止状态。"
            )

        if self._stop_check is not None:

            try:

                stopped = bool(self._stop_check())

            except Exception:

                # 停止判据本身出错时，按「已停止」处理，宁可不动。
                stopped = True

            if stopped:

                raise ComputerControlStopped(
                    "任务已被停止。"
                )

    def set_authorization_check(self, fn) -> None:
        """
        注册「当前是否仍有有效执行授权」的判据。

        fn 应返回 (是否有效, 原因)。

        这是控制器侧的最后一道检查，用来缩小
        「授权检查通过 -> 真正发出调用」之间的窗口。

        ⚠️ 它**不能消除**这个窗口：检查完毕到 pyautogui 调用真正发出之间，
        别的线程仍可能撤销授权。要可靠中断在飞操作，只能靠独立执行进程。
        """

        self._auth_check = fn

    def _check_authorization(self) -> None:
        """
        每个操作执行前调用（紧接 _check_stop 之后）。

        未注册判据时**默认拒绝**。
        """

        if self._auth_check is None:

            raise ComputerControlStopped(
                "没有注册授权判据，拒绝执行任何操作。"
            )

        try:

            result = self._auth_check()

        except Exception:

            raise ComputerControlStopped(
                "授权已失效：授权判据异常"
            )

        # ---------------- 只接受明确的 (bool, str) 二元组 ----------------
        #
        # 第二轮审计 M-A：旧实现用 bool(result) 兜底，
        # 于是 "yes"、1、(True,) 这类真值对象都会被当成「已授权」放行。
        # 现在任何不符合约定的返回值一律拒绝（fail-closed）。
        if not isinstance(result, tuple) or len(result) != 2:

            raise ComputerControlStopped(
                "授权已失效：授权判据必须返回 (bool, str) 二元组。"
            )

        ok, reason = result

        # 必须是真正的 bool —— 排除 1 / 0 这类整数
        if not isinstance(ok, bool):

            raise ComputerControlStopped(
                "授权已失效：授权判据的第一项必须是 bool。"
            )

        if not isinstance(reason, str):

            raise ComputerControlStopped(
                "授权已失效：授权判据的第二项必须是 str。"
            )

        if not ok:

            raise ComputerControlStopped(
                f"授权已失效：{reason}"
            )

    def reset_stop(self, user_acknowledged: bool = False) -> None:
        """
        解除本地硬停止标记。

        紧急停止（emergency_stop）之后**不允许**由普通的任务启动流程
        自动清除：必须由调用方显式传 user_acknowledged=True，
        而这个参数只应来自「用户明确要求恢复电脑控制」这一动作
        （见 AIClient.enable_computer_control 的 clear_emergency_stop 参数）。

        这样「停止 -> 下一个任务悄悄把停止清掉 -> 继续操作」不可能发生。
        """

        if self._stopped and not user_acknowledged:

            raise ComputerControlStopped(
                "控制器处于紧急停止状态，"
                "必须经过明确的恢复流程才能重新执行。"
            )

        self._stopped = False

    def is_emergency_stopped(self) -> bool:
        """当前是否因紧急停止而处于锁定状态。"""

        return bool(self._stopped)

    def is_stopped(self) -> bool:
        """当前是否处于停止状态。"""

        return bool(self._stopped)

    def release_all(self) -> list:
        """
        释放所有被按下的鼠标键。

        无论操作是成功、失败还是被停止，调用方都应当在收尾阶段调用它，
        避免鼠标卡在按下状态。
        """

        released = []

        for button in list(self._pressed):

            try:

                pyautogui.mouseUp(button=button)

                released.append(button)

            except Exception:

                pass

        self._pressed.clear()

        return released

    def pressed_buttons(self) -> list:
        """当前被认为处于按下状态的鼠标键（用于测试与诊断）。"""

        return sorted(self._pressed)

    def emergency_stop(self) -> None:
        """
        紧急停止。

        真正的停止由两件事共同保证：
          1. 置位本地硬停止标记 —— 之后所有操作都会先抛 ComputerControlStopped
          2. 立即释放所有按下的鼠标键 —— 不留卡住的按键

        PyAutoGUI 自身的 FAILSAFE（鼠标甩到屏幕左上角）依然有效，作为第二道保险。
        """

        self._stopped = True

        try:

            self.release_all()

        except Exception:

            pass

        raise ComputerControlStopped(
            "AronaClient computer control 已紧急停止。"
        )


# ================================================================
# 简单测试
# ================================================================

def test_controller() -> None:
    """
    测试电脑控制模块。

    这里只测试：
        - 获取屏幕尺寸
        - 获取鼠标位置
        - 截图

    不会自动点击任何东西。
    """

    print("=" * 50)
    print("AronaClient Computer Controller")
    print("=" * 50)

    controller = ComputerController()

    # 这是手动运行本文件时的本地自测，不经过任何任务流程，
    # 因此显式注册一个放行判据。
    # ⚠️ 只在本文件的 __main__ 自测里这样做；正常运行时由 AIClient 注册真实判据。
    controller.set_authorization_check(lambda: (True, "本地自测"))

    # 屏幕尺寸
    width, height = (
        controller.get_screen_size()
    )

    print(
        f"屏幕尺寸：{width} x {height}"
    )

    # 鼠标位置
    mouse_x, mouse_y = (
        controller.get_mouse_position()
    )

    print(
        f"当前鼠标位置：({mouse_x}, {mouse_y})"
    )

    # 截图
    screenshot_path = (
        controller.capture_screen()
    )

    print(
        f"桌面截图已保存："
        f"{screenshot_path}"
    )

    print("=" * 50)
    print("测试完成")
    print("=" * 50)


if __name__ == "__main__":
    try:
        test_controller()

    except pyautogui.FailSafeException:
        print(
            "检测到 PyAutoGUI 紧急停止。"
        )

    except Exception as exc:
        print(
            "电脑控制模块测试失败："
        )

        print(exc)

        import traceback

        traceback.print_exc()