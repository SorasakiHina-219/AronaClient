import base64
import mimetypes
import time
from pathlib import Path

import cv2

from openai import OpenAI

from config import (
    DEEPSEEK_BASE_URL,
    OPENROUTER_BASE_URL,
    SYSTEM_PROMPT,
)

from core.settings import SettingsManager
from core.memory import MemoryManager
from core.conversation import ConversationManager
from core.computer_control import ComputerController
from core.desktop_vision import (
    check_desktop_vision_authorization,
    set_desktop_vision_authorization,
)

# 门禁留痕函数（下划线开头，仅模块内使用）
from core.desktop_vision import (
    _note_desktop_vision_denied,
)


MAX_IMAGE_SIZE = 32 * 1024 * 1024
MAX_VIDEO_SIZE = 200 * 1024 * 1024

MAX_VIDEO_FRAMES = 8
MAX_VIDEO_FRAME_SIZE = 1280
VIDEO_JPEG_QUALITY = 80

DEEPSEEK_VISION_MODEL = "deepseek-flash"

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
}


class ExecutionGrant:
    """
    一次性的执行授权。

    只在 ComputerTaskManager.certify_plan() 出具凭据之后由
    AIClient._begin_execution() 创建，并且**始终回指那一份计划**。

    因此它不是一张「开了就有效」的通行证：
    计划一旦被撤销批准、内容被改动、不再是活动计划或被停止，
    授权会立刻失效（见 check()）。
    """

    def __init__(self, actions, timeout, plan, manager, started_at=None):
        self.actions = {str(a) for a in actions}

        # 超时起点：计划开始执行的那一刻。
        self.started_at = (
            time.monotonic() if started_at is None else float(started_at)
        )
        self.deadline = self.started_at + float(timeout)

        # 绑定关系：没有计划就没有授权。
        self.plan = plan
        self.manager = manager
        self.closed = False

    def is_expired(self):
        return time.monotonic() > self.deadline

    def allows(self, action):
        return str(action) in self.actions

    def close(self):
        """永久关闭这张授权。"""

        self.closed = True
        self.plan = None
        self.manager = None
        self.actions = set()

    def check(self):
        """
        实时校验授权是否仍然有效。

        返回 (是否有效, 原因)。任何一项不满足都不允许继续执行。
        """

        if self.closed:
            return False, "执行授权已关闭。"

        if self.plan is None or self.manager is None:
            return False, "执行授权没有绑定到有效计划。"

        if self.is_expired():
            return False, "任务已超时，拒绝继续执行。"

        if self.plan is not self.manager._active_plan:
            return False, "计划已不再是当前活动计划。"

        if not self.plan.approved:
            return False, "任务授权已撤销。"

        try:
            if self.plan.approved_fingerprint != self.plan.fingerprint():
                return False, "计划内容在批准之后被修改，原授权失效。"
        except Exception:
            return False, "计划指纹无法校验。"

        if self.manager._stop_event.is_set():
            return False, "任务已被停止。"

        return True, ""


class AIClient:

    def __init__(self):

        self.settings = SettingsManager()
        self.memory = MemoryManager()
        self.conversations = ConversationManager()

        self.client = None

        self.provider = None
        self.model = None

        self.messages = []

        self.current_session = None

        # ====================================================
        # Computer Control
        # ====================================================

        # 底层控制器。
        #
        # 刻意保持私有：公开出去就等于开了一条绕过审批的通道。
        # 想拿它请走 ComputerTaskManager。
        self._computer = ComputerController()

        # ====================================================
        # 桌面视觉授权（方案 B：独立开关，默认关闭）
        # ====================================================
        #
        # 桌面视觉【不】复用电脑控制开关的语义：
        #   * 电脑控制开关 -> 能不能模拟鼠标键盘
        #   * 桌面视觉开关 -> 能不能读取桌面像素与前台窗口信息
        #
        # 两者互相独立：只开一个不会顺带放开另一个。
        #
        # 判据注册在 AIClient 构造时完成；在它注册之前，
        # 模块级门禁处于「未注册 -> 默认拒绝」状态。
        # allow_replace=True 是【显式】的：
        # 新建 AIClient 属于合法的重新初始化。
        # 其他代码若想覆盖已注册的判据，必须同样显式声明，
        # 不能悄悄换掉授权逻辑。
        set_desktop_vision_authorization(
            self._desktop_vision_snapshot,
            allow_replace=True,
        )

        # 控制器侧的最后一道授权检查。
        #
        # 它缩小「授权检查通过 -> 真正发出 pyautogui 调用」之间的窗口，
        # 让 disable_computer_control() / _end_execution() / plan.approved=False
        # 这类只动授权的撤销也能在调用发出前被察觉。
        #
        # ⚠️ 它**不能消除**该竞态：检查与调用之间仍有窗口。
        # 可靠中断在飞操作需要独立执行进程。
        try:

            self._computer.set_authorization_check(
                self._authorization_snapshot
            )

        except Exception:

            pass

        # 安全开关。
        #
        # 默认关闭，且**单独打开它不足以执行任何操作** ——
        # 每个动作还要同时满足「存在已批准的任务 + 动作在白名单内 +
        # 未停止 + 未超时」，见 _authorize()。
        #
        # 刻意用私有名保存：对外只暴露只读属性，直接赋值会被拒绝，
        # 必须走 enable_computer_control() / disable_computer_control()。
        self._computer_control_enabled = False

        # 当前这一次执行的授权（由 ComputerTaskManager 打开/关闭）。
        self._grant = None

        # 外部停止判据（由 ComputerTaskManager 注入）。
        self._stop_signal = None

        # 联网视觉分析是否已获得用户明确授权。
        #
        # 截图本身是本地只读操作；但把截图传给模型会消耗 API 额度，
        # 必须由调用方显式声明「用户已经确认过」。
        self.vision_upload_confirmed = False

        # 电脑任务管理器（按需创建，避免循环导入）。
        #
        # 同样刻意用私有名保存：它是授权的唯一来源，
        # 如果允许外部替换，就等于允许伪造授权（第三轮审计 M-1）。
        self._task_manager = None

        self.reload()

        self._restore_current_session()

    # ========================================================
    # Settings
    # ========================================================

    def reload(self):

        self.provider = (
            self.settings.get_provider()
        )

        self.model = (
            self.settings.get_model(
                self.provider
            )
        )

        api_key = (
            self.settings.get_api_key(
                self.provider
            )
        )

        if self.provider == "deepseek":

            base_url = DEEPSEEK_BASE_URL

        elif self.provider == "openrouter":

            base_url = OPENROUTER_BASE_URL

        else:

            base_url = None

        if api_key and base_url:

            self.client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )

        else:

            self.client = None

        self._ensure_system_message()

    def _ensure_system_message(self):

        if not self.messages:

            self.messages = [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                }
            ]

    def get_provider(self):

        return self.provider

    def get_model(self):

        return self.model

    def is_configured(self):

        return (
            self.client is not None
            and bool(self.model)
        )

    # ========================================================
    # Computer Control
    # ========================================================

    @property
    def computer_control_enabled(self):

        """
        总开关的**只读**视图。

        直接赋值会被拒绝，避免任何代码用
        `ai_client.computer_control_enabled = True` 绕过开关管理。
        要改变它请调用 enable_computer_control() / disable_computer_control()。
        """

        return self._computer_control_enabled

    @computer_control_enabled.setter
    def computer_control_enabled(self, value):

        raise PermissionError(
            "不允许直接给 computer_control_enabled 赋值。"
            "请使用 enable_computer_control() / disable_computer_control()。"
        )

    def enable_computer_control(self, clear_emergency_stop=False):

        """
        开启鼠标和键盘控制。

        这是解除紧急停止的**唯一受控入口**：
        只有明确传入 clear_emergency_stop=True，
        才会一并清除紧急停止状态；普通的任务启动流程不会。

        注意：
        开启开关本身不会执行任何操作，也不构成对某个计划的批准。
        """

        if self._computer.is_stopped() and not clear_emergency_stop:

            raise PermissionError(
                "控制器处于紧急停止状态。"
                "如确需恢复，请显式调用 "
                "enable_computer_control(clear_emergency_stop=True)。"
            )

        if clear_emergency_stop:

            try:

                self._computer.reset_stop(user_acknowledged=True)

            except Exception:

                pass

        self._computer_control_enabled = True

    def disable_computer_control(self):

        """
        关闭鼠标和键盘控制，并立即作废当前执行授权。
        """

        self._computer_control_enabled = False

        self._end_execution()

    def is_computer_control_enabled(self):

        return self._computer_control_enabled

    @property
    def computer(self):
        """
        底层控制器**不再对外公开**。

        保留这个属性是为了让「试图直接拿控制器」的代码得到一条明确错误，
        而不是悄悄绕过权限与审批。请改用 ComputerTaskManager。
        """

        raise PermissionError(
            "不再允许直接访问底层控制器。"
            "请通过 ComputerTaskManager 创建计划、获得用户批准后再执行。"
        )

    def _bind_stop_signal(self, fn):

        """
        注入外部停止判据。

        ComputerTaskManager 会把自己的停止信号接进来，
        这样执行过程中一旦要求停止，控制器会在下一个操作前拒绝执行。
        """

        self._stop_signal = fn

        try:

            self._computer.set_stop_check(fn)

        except Exception:

            pass

    def _begin_execution(self, plan, timeout=30.0):

        """
        打开一次执行授权。

        **只接受一个「计划对象」**，不接受动作名列表。

        动作列表由 ComputerTaskManager.certify_plan(plan) 现场出具，
        而 certify_plan 会逐项校验：

            * 是当前活动计划
            * 已获得用户批准
            * 批准指纹与当前内容一致（参数被改过就作废）
            * 每个动作都在白名单内且参数合法
            * 任务未被停止
            * 没有另一个任务正在执行

        因此**无法凭一份自拟的动作名列表凭空开出授权**，
        也就无法为 click_mouse / type_text 等白名单外的动作开权限。

        另外这里**不会**调用 reset_stop()：
        紧急停止必须由用户通过 enable_computer_control(clear_emergency_stop=True)
        明确恢复，普通任务启动流程不允许静默清除（修复 H3）。
        """

        if self._computer.is_stopped():

            raise PermissionError(
                "控制器处于紧急停止状态，拒绝打开执行授权。"
                "必须由用户明确恢复后才能重新执行。"
            )

        # ---------------- 授权来源三重校验（M-1）----------------
        #
        # 审计发现：把 task_manager 换成伪造对象即可凭空开授权。
        # 这里逐层收紧：
        #   1. 必须是本客户端的**那一个**管理器实例
        #   2. 必须是真正的 ComputerTaskManager（挡鸭子类型伪造）
        #   3. 计划必须是真正的 ComputerTaskPlan
        from core.computer_task import (
            ComputerTaskManager,
            ComputerTaskPlan,
        )

        manager = self._task_manager

        if manager is None:

            raise PermissionError(
                "没有任务管理器，拒绝打开执行授权。"
            )

        if manager is not self.get_task_manager():

            raise PermissionError(
                "授权来源不是本客户端的任务管理器，拒绝打开执行授权。"
            )

        if not isinstance(manager, ComputerTaskManager):

            raise PermissionError(
                "授权的来源类型不合法，拒绝打开执行授权。"
            )

        if not isinstance(plan, ComputerTaskPlan):

            raise PermissionError(
                "计划不是合法的 ComputerTaskPlan 实例，拒绝打开执行授权。"
            )

        # 由管理器出具凭据；校验不通过会直接抛异常。
        actions = manager.certify_plan(plan)

        started_at = time.monotonic()

        self._grant = ExecutionGrant(
            actions,
            timeout,
            plan=plan,
            manager=manager,
            started_at=started_at,
        )

        return started_at

    def _end_execution(self):

        """关闭并作废当前执行授权。"""

        grant = self._grant

        self._grant = None

        if grant is not None:

            try:

                grant.close()

            except Exception:

                pass

    def release_all_inputs(self):

        """
        释放所有被按下的鼠标键。

        任务无论成功、失败还是被停止，收尾时都应调用它。
        """

        try:

            return self._computer.release_all()

        except Exception:

            return []

    def is_execution_granted(self):

        """当前是否存在有效的执行授权。"""

        grant = self._grant

        return bool(grant is not None and not grant.is_expired())

    def _authorize(self, action):

        """
        统一权限闸门。

        所有通向 pyautogui 的路径都必须先过这里。要同时满足：

          1. 电脑控制总开关已打开
          2. 存在一次已获批准的执行授权
          3. 授权未超时
          4. 任务未被停止
          5. 该动作在本次授权的白名单内

        任何一条不满足都抛 PermissionError，绝不执行。
        """

        if not self._computer_control_enabled:

            raise PermissionError(
                "电脑控制功能目前处于关闭状态。"
            )

        grant = self._grant

        if grant is None:

            raise PermissionError(
                "当前没有已获批准的任务，拒绝执行任何操作。"
            )

        # 实时校验：授权是否仍绑定在一份「有效、已批准、未被改动」的计划上。
        ok, reason = grant.check()

        if not ok:

            raise PermissionError(reason)

        if self._stop_signal is not None:

            try:

                stopped = bool(self._stop_signal())

            except Exception:

                stopped = True

            if stopped:

                raise PermissionError(
                    "任务已被停止，拒绝继续执行。"
                )

        if not grant.allows(action):

            raise PermissionError(
                f"当前任务未批准执行 {action!r}。"
            )

        return True

    def _desktop_vision_snapshot(self):
        """
        桌面视觉判据。

        fail-closed：任何不确定的情况都返回 False。

        返回 (ok, reason)。
        """

        try:

            if not self.settings.get_desktop_vision_enabled():

                return False, "桌面视觉功能处于关闭状态。"

            return True, ""

        except Exception:

            return False, "无法确认桌面视觉开关状态。"

    def _authorization_snapshot(self):
        """
        供 ComputerController 调用的授权判据。

        返回 (是否有效, 原因)。只读，不改任何状态。
        """

        if not self._computer_control_enabled:

            return False, "电脑控制功能处于关闭状态"

        grant = self._grant

        if grant is None:

            return False, "没有执行授权"

        try:

            return grant.check()

        except Exception:

            return False, "授权校验异常"

    def _require_computer_control(self, action=None):

        """
        兼容旧调用。

        不带参数时只检查总开关；
        带 action 时走完整的 _authorize() 闸门。
        """

        if action is not None:

            return self._authorize(action)

        if not self._computer_control_enabled:

            raise PermissionError(
                "电脑控制功能目前处于关闭状态。"
                "请先调用 enable_computer_control()。"
            )

        return True

    def get_screen_size(self):

        """
        获取当前电脑主屏幕尺寸。

        返回：
            (width, height)
        """

        self._authorize("get_screen_size")

        return self._computer.get_screen_size()

    def get_mouse_position(self):

        """
        获取当前鼠标位置。

        返回：
            (x, y)
        """

        # 统一走闸门；第一阶段白名单未包含它，因此默认会被拒绝。
        self._authorize("get_mouse_position")

        return self._computer.get_mouse_position()

    def get_desktop_screenshot(
        self,
        save_path=None,
    ):

        """
        截取当前电脑桌面。

        这个操作不会控制电脑，
        因此不需要开启 computer_control。

        ★ ARONASEC-F5-FIX-001（产品规则）：
          desktop_vision 是独立的桌面隐私权限。
          dv 关闭时，**所有**截图行为均拒绝 ——
          电脑控制（cc）与有效的任务 grant **均不得覆盖**这一限制。

          门禁必须放在最前面：拒绝时不得进入截图执行路径。
        """

        # ---------------- dv 门禁：必须在 _authorize 与任何截图之前 ----------------
        #
        # 复用 core.desktop_vision 的既有判据（不重新实现授权逻辑）。
        # 该判据 fail-closed：未注册 / 判据异常 / 返回值畸形 -> 一律拒绝。
        _dv_ok, _dv_reason = check_desktop_vision_authorization()

        if not _dv_ok:

            _note_desktop_vision_denied(
                "ai_client.get_desktop_screenshot", _dv_reason
            )

            # 与 get_active_window_info() 的拒绝风格保持一致：**抛出**而不是静默返回，
            # 让调用方能够区分「被拒绝」与「截图失败」。
            raise PermissionError(
                f"截取桌面需要开启桌面感知：{_dv_reason}"
            )

        # ---------------- 既有任务授权（保持不变）----------------
        self._authorize("capture_screen")

        return self._computer.capture_screen(
            save_path=save_path
        )

    def get_active_window_info(self):

        """
        获取当前活动窗口的信息。

        这是**本地只读**操作：不移动鼠标、不按键、不联网、不消耗 API 额度。

        ⚠️ 但它读取的是**桌面感知数据**（前台窗口标题 + 进程镜像路径），
        因此必须要求独立的 desktop_vision_enabled 开关。

        仅开启电脑控制**不足以**授权本项：
            电脑控制 -> 能不能模拟鼠标键盘
            桌面感知 -> 能不能读取桌面与前台窗口信息
        """

        # ---------------- 门禁：在任何 Win32 读取之前 ----------------
        #
        # 放在最前面，保证「未授权 -> 拒绝」这条路径
        # 不会先调用 OpenProcess / GetForegroundWindow 再拒绝。
        #
        # 底层 computer.window_info.get_active_window() 也有一道同样的
        # 门禁（computer.desktop_access），这里是第二道。
        _dv_ok, _dv_reason = check_desktop_vision_authorization()

        if not _dv_ok:

            _note_desktop_vision_denied(
                "ai_client.get_active_window_info", _dv_reason
            )

            # 与 _authorize() 的拒绝风格保持一致：**抛出**而不是静默返回 None。
            # 静默返回会让调用方无法区分「没有前台窗口」与「没有权限」。
            raise PermissionError(
                f"读取前台窗口信息需要开启桌面感知：{_dv_reason}"
            )

        self._authorize("get_active_window")

        try:

            from computer.window_info import get_active_window

            info = get_active_window()

            if isinstance(info, dict) and info.get("success"):

                return info.get("window")

        except Exception:

            pass

        return None
    def list_windows(self, limit=20):

        """
        枚举当前桌面上可见且有标题的顶层窗口。

        ★ ARONA-NIGHT-001：这是**纯只读**能力。

        与 get_active_window_info 保持一致的地方：
          * 同样要求独立的 desktop_vision_enabled 开关
          * 门禁在**任何**底层 Win32 调用之前
          * 未授权时**抛出 PermissionError**，不静默返回空

        与 get_active_window_info **故意不同**的地方：
          * **不吞异常** —— 底层失败向上传播，不伪装成 None / []
          * 因此调用方可以区分「没有窗口」（返回 []）
            与「读取失败」（抛异常）

        明确**不做**的事：
          * 不要求开启 computer_control（只读，不碰鼠标键盘）
          * 不注册、不替换、不重置全局 provider
          * 不把窗口标题自动发送给模型
          * 不写日志、不落盘
          * 不接入计划执行器，不新增动作白名单
        """

        # ---------------- 门禁：在任何 Win32 读取之前 ----------------
        _dv_ok, _dv_reason = check_desktop_vision_authorization()

        if not _dv_ok:

            _note_desktop_vision_denied(
                "ai_client.list_windows", _dv_reason
            )

            raise PermissionError(
                f"读取窗口列表需要开启桌面感知：{_dv_reason}"
            )

        # 参数错误尽早报告（底层会再次校验，这里是第二道）
        if isinstance(limit, bool) or not isinstance(limit, int):

            raise TypeError("limit 必须是整数。")

        if limit <= 0:

            raise ValueError("limit 必须大于 0。")

        from computer.window_info import list_visible_windows

        # 底层异常**不捕获**，向上传播。
        return list_visible_windows(limit=limit)

    def analyze_desktop(
        self,
        instruction="请分析当前电脑桌面，并告诉我你看到了什么。",
        user_confirmed=False,
    ):

        """
        截取当前桌面，然后交给视觉模型分析。

        ⚠️ 这是**联网操作**：

            截图会被上传给模型，会消耗 API 额度。

        因此必须由调用方显式传入 user_confirmed=True。
        这个参数应当来自界面上「用户已经看过截图并点了确认」，
        不能由代码自行填 True。

        返回：
            AI 的分析结果。
        """

        if not user_confirmed:

            raise PermissionError(
                "把桌面截图上传给模型会消耗 API 额度，"
                "必须由用户明确确认后才能执行。"
            )

        if not self.client:

            raise RuntimeError(
                "尚未配置 AI API Key，请打开设置进行配置。"
            )

        # 本地截图走统一闸门。
        #
        # 这里**不再自行开授权** —— 自行开授权等于自己批准自己，
        # 正是 C1 漏洞的成因。
        # 现在必须由调用方把「截图 + 分析」放进一份经过用户批准的计划里，
        # 在计划执行期间调用本方法；否则 _authorize 会拒绝截图。
        screenshot_path = (
            self.get_desktop_screenshot()
        )

        image_message = (
            self._build_image_message(
                instruction,
                [
                    Path(screenshot_path)
                ],
            )
        )

        request_messages = list(
            self.messages
        )

        request_messages.append(
            image_message
        )

        request_model = (
            self._get_vision_model()
        )

        response = (
            self.client.chat.completions.create(
                model=request_model,
                messages=request_messages,
            )
        )

        answer = (
            response.choices[0]
            .message.content
            or ""
        )

        return answer

    # ========================================================
    # Computer Task API（计划 -> 审批 -> 执行）
    # ========================================================

    @property
    def task_manager(self):

        """
        任务管理器的**只读**视图。

        直接赋值会被拒绝：它是授权的唯一来源，
        允许替换就等于允许伪造授权（第三轮审计 M-1）。
        """

        return self._task_manager

    @task_manager.setter
    def task_manager(self, value):

        raise PermissionError(
            "不允许直接给 task_manager 赋值。"
            "它是执行授权的唯一来源，请使用 get_task_manager()。"
        )

    def get_task_manager(self):

        """取得电脑任务管理器（按需创建）。"""

        if self._task_manager is None:

            from core.computer_task import ComputerTaskManager

            self._task_manager = ComputerTaskManager(self)

        return self._task_manager

    def plan_computer_task(self, payload):

        """
        把模型返回的内容解析成一个计划，**但不执行任何操作**。

        payload 可以是 JSON 文本或 dict。
        解析与校验全部走严格规则，绝不 eval / exec。
        """

        from core.computer_task import parse_plan_payload

        title, goal, actions = parse_plan_payload(payload)

        return self.get_task_manager().create_plan(
            title,
            goal,
            actions,
        )

    def approve_computer_plan(self, plan, ticket):

        """
        批准某一份计划。

        ⚠️ 保护范围必须如实理解：

        `ticket` 是**显式调用标志**，不是不可伪造的用户身份凭据。
        任何能执行 Python 的代码都可以自己构造一张 ApprovalTicket 并传入。
        它的作用是：
          * 让「批准」必须显式携带一张绑定到该计划的凭据
          * 让自我批准在代码审查里一眼可见
          * 配合 nonce 消费，杜绝同一份批准被重复使用

        它**不能**防御同进程任意 Python 代码。
        """

        self.get_task_manager().approve_plan(plan, ticket)

    def run_computer_plan(self, plan, ticket):

        """
        执行一份已获批准的计划。

        必须携带**与批准时同一张**凭据。
        应当从后台线程调用；没有批准或凭据不符时一定会被拒绝。
        """

        return self.get_task_manager().execute_plan(plan, ticket)

    def cancel_computer_plan(self, plan=None):

        """取消待执行的计划。"""

        self.get_task_manager().cancel_plan(plan)

    def stop_computer_task(self):

        """请求停止当前正在执行的电脑任务。"""

        self.get_task_manager().request_stop()

    # ========================================================
    # Mouse Control
    # ========================================================

    def _check_screen_bounds(self, x, y):
        """
        按**实际屏幕尺寸**校验坐标（修复 M1）。

        之前只比对固定的 0..10000，等于没校验：
        在 1366x768 的屏幕上，(9999, 9999) 也能通过。

        这里刻意**不做夹取**（不静默把越界坐标改成屏幕边缘）——
        越界一律报错，避免把「计划写错了」伪装成「执行成功了」。
        """

        try:

            width, height = self._computer.get_screen_size()

        except Exception as exc:

            raise ValueError(
                f"无法获取屏幕尺寸，拒绝移动鼠标：{exc}"
            )

        width = int(width)
        height = int(height)

        if not (0 <= int(x) < width and 0 <= int(y) < height):

            raise ValueError(
                f"坐标 ({int(x)}, {int(y)}) 超出屏幕范围 "
                f"{width}x{height}，拒绝执行。"
            )

        return int(x), int(y)

    def move_mouse(
        self,
        x,
        y,
        duration=0.2,
    ):

        """
        移动鼠标。

        必须先开启 enable_computer_control()，
        并且处于一次已获批准的计划执行之中。

        坐标会按**实际屏幕尺寸**校验，越界拒绝执行（不夹取）。
        """

        # 先过闸门，再做边界校验。
        #
        # 曾试过把边界校验提前来收窄 TOCTOU 窗口（第三轮审计 M-2），
        # 但那会让「未授权时也会调用 pyautogui.size()」——
        # 用一条新的未闸门通路去换一个本来就无法消除的竞态，得不偿失，故撤回。
        #
        # M-2 如实记录为**无法在进程内消除**的竞态：
        # 授权检查通过后到真正发出调用之间必然存在窗口。
        self._authorize("move_mouse")

        cx, cy = self._check_screen_bounds(x, y)

        self._computer.move_mouse(
            x=cx,
            y=cy,
            duration=float(duration),
        )

    def click_mouse(
        self,
        x=None,
        y=None,
        button="left",
    ):

        """
        鼠标点击。

        button：
            left
            right
            middle
        """

        self._authorize("click_mouse")

        self._computer.click(
            x=x,
            y=y,
            button=button,
            clicks=1,
        )

    def double_click_mouse(
        self,
        x=None,
        y=None,
    ):

        """
        鼠标左键双击。
        """

        self._authorize("double_click_mouse")

        self._computer.double_click(
            x=x,
            y=y,
        )

    def right_click_mouse(
        self,
        x=None,
        y=None,
    ):

        """
        鼠标右键。
        """

        self._authorize("right_click_mouse")

        self._computer.right_click(
            x=x,
            y=y,
        )

    # ========================================================
    # Keyboard Control
    # ========================================================

    def type_text(
        self,
        text,
        interval=0.02,
    ):

        """
        输入文字。

        注意：
        PyAutoGUI 原生输入对中文支持有限。
        英文、数字和常见符号可以直接输入。
        """

        self._authorize("type_text")

        self._computer.type_text(
            text=text,
            interval=interval,
        )

    def press_key(
        self,
        key,
        presses=1,
        interval=0.05,
    ):

        """
        按键。

        例如：

            press_key("enter")
            press_key("esc")
            press_key("space")
            press_key("tab")
        """

        self._authorize("press_key")

        self._computer.press_key(
            key=key,
            presses=presses,
            interval=interval,
        )

    def hotkey(
        self,
        *keys,
    ):

        """
        组合键。

        例如：

            hotkey("ctrl", "c")
            hotkey("ctrl", "v")
            hotkey("alt", "tab")
        """

        self._authorize("hotkey")

        self._computer.hotkey(
            *keys
        )

    # ========================================================
    # Mouse Wheel
    # ========================================================

    def scroll(
        self,
        amount,
    ):

        """
        鼠标滚轮。

        正数：
            向上

        负数：
            向下
        """

        self._authorize("scroll")

        self._computer.scroll(
            amount
        )

    # ========================================================
    # Conversation Restore
    # ========================================================

    def _restore_current_session(self):

        session = self.conversations.ensure_session()

        if not session:

            self._ensure_system_message()

            return

        self.current_session = session

        stored_messages = session.get(
            "messages",
            [],
        )

        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        for message in stored_messages:

            if not isinstance(
                message,
                dict,
            ):

                continue

            role = message.get("role")

            content = message.get("content")

            if role not in (
                "user",
                "assistant",
            ):

                continue

            if not isinstance(
                content,
                str,
            ):

                continue

            if not content.strip():

                continue

            self.messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    def _conversation_messages(self):

        result = []

        for message in self.messages:

            role = message.get("role")

            if role not in (
                "user",
                "assistant",
            ):

                continue

            content = message.get(
                "content",
                "",
            )

            if not isinstance(
                content,
                str,
            ):

                continue

            if not content.strip():

                continue

            result.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        return result

    def _make_session_title(
        self,
        message,
    ):

        text = (message or "").strip()

        if not text:

            return "新对话"

        text = " ".join(
            text.split()
        )

        if len(text) > 24:

            text = (
                text[:24]
                + "…"
            )

        return text

    def _save_current_session(self):

        if not self.current_session:

            return

        session_id = (
            self.current_session.get("id")
        )

        if not session_id:

            return

        title = (
            self.current_session.get(
                "title"
            )
            or "新对话"
        )

        messages = (
            self._conversation_messages()
        )

        self.conversations.update_messages(
            session_id,
            messages,
            title=title,
        )

        refreshed = (
            self.conversations.get_session(
                session_id
            )
        )

        if refreshed:

            self.current_session = refreshed

    # ========================================================
    # Connection Test
    # ========================================================

    def test_connection(
        self,
        provider=None,
        model=None,
        api_key=None,
    ):

        provider = (
            provider
            or self.provider
        )

        model = (
            model
            or self.model
        )

        if api_key is None:

            api_key = (
                self.settings.get_api_key(
                    provider
                )
            )

        if provider == "deepseek":

            base_url = DEEPSEEK_BASE_URL

        elif provider == "openrouter":

            base_url = OPENROUTER_BASE_URL

        else:

            raise ValueError(
                f"不支持的 AI 服务商：{provider}"
            )

        test_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

        response = (
            test_client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": "你好，请回复“连接成功”。",
                    }
                ],
                max_tokens=20,
            )
        )

        return (
            response.choices[0]
            .message.content
            or "连接成功"
        )

    # ========================================================
    # Vision
    # ========================================================

    def _get_vision_model(self):

        if self.provider == "deepseek":

            return DEEPSEEK_VISION_MODEL

        return self.model

    def _attachment_to_path(
        self,
        attachment,
    ):

        if isinstance(
            attachment,
            (str, Path),
        ):

            return Path(attachment)

        if isinstance(
            attachment,
            dict,
        ):

            for key in (
                "path",
                "file_path",
                "filepath",
                "file",
                "filename",
            ):

                value = attachment.get(
                    key
                )

                if value:

                    return Path(value)

        return None

    def _normalize_attachments(
        self,
        attachments,
    ):

        result = []

        for attachment in (
            attachments or []
        ):

            path = self._attachment_to_path(
                attachment
            )

            if path is not None:

                result.append(path)

        return result

    def _detect_image_mime(
        self,
        path,
    ):

        suffix = path.suffix.lower()

        if suffix in IMAGE_MIME_TYPES:

            return IMAGE_MIME_TYPES[suffix]

        mime_type, _ = (
            mimetypes.guess_type(
                str(path)
            )
        )

        if (
            mime_type
            and mime_type.startswith(
                "image/"
            )
        ):

            return mime_type

        return None

    def _image_to_data_url(
        self,
        path,
    ):

        if not path.exists():

            raise FileNotFoundError(
                f"图片不存在：{path}"
            )

        if path.stat().st_size > MAX_IMAGE_SIZE:

            raise ValueError(
                f"图片过大：{path.name}"
            )

        mime_type = (
            self._detect_image_mime(
                path
            )
        )

        if not mime_type:

            raise ValueError(
                f"不支持的图片格式：{path.name}"
            )

        with open(
            path,
            "rb",
        ) as file:

            encoded = base64.b64encode(
                file.read()
            ).decode("utf-8")

        return (
            f"data:{mime_type};base64,{encoded}"
        )

    def _get_image_attachments(
        self,
        paths,
    ):

        return [
            path
            for path in paths
            if self._detect_image_mime(path)
        ]

    def _build_image_message(
        self,
        text,
        image_paths,
    ):

        content = []

        if text:

            content.append(
                {
                    "type": "text",
                    "text": text,
                }
            )

        for path in image_paths:

            data_url = (
                self._image_to_data_url(
                    path
                )
            )

            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": data_url,
                        "detail": "auto",
                    },
                }
            )

        return {
            "role": "user",
            "content": content,
        }

    # ========================================================
    # Video
    # ========================================================

    def _is_video(
        self,
        path,
    ):

        return (
            path.suffix.lower()
            in VIDEO_EXTENSIONS
        )

    def _extract_video_frames(
        self,
        path,
    ):

        if not path.exists():

            raise FileNotFoundError(
                f"视频不存在：{path}"
            )

        if path.stat().st_size > MAX_VIDEO_SIZE:

            raise ValueError(
                f"视频过大：{path.name}"
            )

        capture = cv2.VideoCapture(
            str(path)
        )

        if not capture.isOpened():

            raise ValueError(
                f"无法打开视频：{path.name}"
            )

        total_frames = int(
            capture.get(
                cv2.CAP_PROP_FRAME_COUNT
            )
        )

        fps = (
            capture.get(
                cv2.CAP_PROP_FPS
            )
            or 0
        )

        if total_frames <= 0:

            capture.release()

            raise ValueError(
                f"无法读取视频帧：{path.name}"
            )

        duration = (
            total_frames / fps
            if fps > 0
            else 0
        )

        frame_count = min(
            MAX_VIDEO_FRAMES,
            total_frames,
        )

        if frame_count <= 1:

            positions = [0]

        else:

            positions = [
                int(
                    i
                    * (total_frames - 1)
                    / (frame_count - 1)
                )
                for i in range(
                    frame_count
                )
            ]

        frames = []

        for position in positions:

            capture.set(
                cv2.CAP_PROP_POS_FRAMES,
                position,
            )

            success, frame = (
                capture.read()
            )

            if not success:

                continue

            height, width = frame.shape[:2]

            longest_side = max(
                width,
                height,
            )

            if (
                longest_side
                > MAX_VIDEO_FRAME_SIZE
            ):

                scale = (
                    MAX_VIDEO_FRAME_SIZE
                    / longest_side
                )

                new_width = max(
                    1,
                    int(width * scale),
                )

                new_height = max(
                    1,
                    int(height * scale),
                )

                frame = cv2.resize(
                    frame,
                    (
                        new_width,
                        new_height,
                    ),
                    interpolation=cv2.INTER_AREA,
                )

            success, encoded = cv2.imencode(
                ".jpg",
                frame,
                [
                    cv2.IMWRITE_JPEG_QUALITY,
                    VIDEO_JPEG_QUALITY,
                ],
            )

            if not success:

                continue

            base64_data = (
                base64.b64encode(
                    encoded.tobytes()
                ).decode("utf-8")
            )

            frames.append(
                {
                    "data_url": (
                        "data:image/jpeg;base64,"
                        + base64_data
                    ),
                    "frame": position,
                }
            )

        capture.release()

        return frames, duration

    def _build_video_message(
        self,
        text,
        frames,
        duration,
    ):

        content = []

        if text:

            content.append(
                {
                    "type": "text",
                    "text": text,
                }
            )

        description = (
            "\n这是一个视频附件。"
            f"视频时长约 {duration:.1f} 秒。"
            f"下面是从视频中提取的 "
            f"{len(frames)} 个关键画面。"
        )

        content.append(
            {
                "type": "text",
                "text": description,
            }
        )

        for frame in frames:

            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": frame["data_url"],
                        "detail": "auto",
                    },
                }
            )

        return {
            "role": "user",
            "content": content,
        }

    def _analyze_video(
        self,
        text,
        video_paths,
    ):

        all_frames = []

        total_duration = 0

        for path in video_paths:

            frames, duration = (
                self._extract_video_frames(
                    path
                )
            )

            all_frames.extend(
                frames
            )

            total_duration += duration

        return self._build_video_message(
            text,
            all_frames,
            total_duration,
        )

    # ========================================================
    # Normal Files
    # ========================================================

    def _build_attachment_text(
        self,
        paths,
    ):

        if not paths:

            return ""

        lines = [
            "用户附加了以下文件："
        ]

        for path in paths:

            lines.append(
                f"- {path.name}"
            )

        return "\n".join(lines)

    # ========================================================
    # Model
    # ========================================================

    def _select_model(
        self,
        has_visual_input,
    ):

        if has_visual_input:

            return self._get_vision_model()

        return self.model

    # ========================================================
    # Send Message
    # ========================================================

    def send_message(
        self,
        message,
        attachments=None,
        save_to_session=True,
    ):

        if not self.client:

            raise RuntimeError(
                "尚未配置 AI API Key，请打开设置进行配置。"
            )

        paths = (
            self._normalize_attachments(
                attachments
            )
        )

        image_paths = (
            self._get_image_attachments(
                paths
            )
        )

        video_paths = [
            path
            for path in paths
            if self._is_video(path)
        ]

        other_paths = [
            path
            for path in paths
            if path not in image_paths
            and path not in video_paths
        ]

        memory_context = (
            self.memory.build_memory_context()
        )

        final_text = message or ""

        if memory_context:

            final_text = (
                memory_context
                + "\n\n"
                + final_text
            )

        has_visual_input = bool(
            image_paths
            or video_paths
        )

        if video_paths:

            request_message = (
                self._analyze_video(
                    final_text,
                    video_paths,
                )
            )

        elif image_paths:

            request_message = (
                self._build_image_message(
                    final_text,
                    image_paths,
                )
            )

        else:

            if other_paths:

                attachment_text = (
                    self._build_attachment_text(
                        other_paths
                    )
                )

                if final_text:

                    final_text += (
                        "\n\n"
                        + attachment_text
                    )

                else:

                    final_text = attachment_text

            request_message = {
                "role": "user",
                "content": final_text,
            }

        if save_to_session:

            self.messages.append(
                request_message
            )

            self.memory.auto_remember(
                message or ""
            )

        request_model = (
            self._select_model(
                has_visual_input
            )
        )

        # 使用临时消息列表发送请求，避免感知请求污染会话上下文。
        # save_to_session=True 时使用 self.messages（含历史上下文）；
        # save_to_session=False 时使用仅含本次请求的临时列表。
        context_messages = (
            self.messages + [request_message]
            if save_to_session
            else [request_message]
        )

        response = (
            self.client.chat.completions.create(
                model=request_model,
                messages=context_messages,
            )
        )

        answer = (
            response.choices[0]
            .message.content
            or ""
        )

        if save_to_session:

            self.messages.append(
                {
                    "role": "assistant",
                    "content": answer,
                }
            )

            if self.current_session:

                if (
                    self.current_session.get("title")
                    in (
                        "",
                        None,
                        "新对话",
                    )
                    and message
                ):

                    self.current_session[
                        "title"
                    ] = self._make_session_title(
                        message
                    )

                self._save_current_session()

        return answer

    # ========================================================
    # Conversation Context
    # ========================================================

    def clear_history(self):

        session = self.conversations.create_session(
            title="新对话"
        )

        self.current_session = session

        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

    def export_text_history(self):

        result = []

        for message in self.messages:

            role = message.get(
                "role"
            )

            if role == "system":

                continue

            content = message.get(
                "content",
                "",
            )

            if isinstance(
                content,
                str,
            ):

                result.append(
                    {
                        "role": role,
                        "text": content,
                    }
                )

            elif isinstance(
                content,
                list,
            ):

                texts = []

                for part in content:

                    if not isinstance(
                        part,
                        dict,
                    ):

                        continue

                    if part.get(
                        "type"
                    ) == "text":

                        texts.append(
                            part.get(
                                "text",
                                "",
                            )
                        )

                result.append(
                    {
                        "role": role,
                        "text": "\n".join(
                            texts
                        ),
                    }
                )

        return result

    def load_conversation(
        self,
        messages,
    ):

        self.clear_history()

        for message in messages:

            role = message.get(
                "role"
            )

            text = (
                message.get(
                    "text",
                    "",
                )
                or ""
            )

            if role not in (
                "user",
                "assistant",
            ):

                continue

            if not text:

                continue

            self.messages.append(
                {
                    "role": role,
                    "content": text,
                }
            )

        self._save_current_session()

    # ========================================================
    # Conversation Manager API
    # ========================================================

    def get_current_session(self):

        return self.current_session

    def get_current_session_id(self):

        if not self.current_session:

            return None

        return self.current_session.get(
            "id"
        )

    def list_conversations(self):

        return self.conversations.list_sessions()

    def create_conversation(
        self,
        title="新对话",
    ):

        session = (
            self.conversations.create_session(
                title=title
            )
        )

        self.current_session = session

        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        return session

    def switch_conversation(
        self,
        session_id,
    ):

        session = (
            self.conversations.get_session(
                session_id
            )
        )

        if not session:

            return False

        self.conversations.set_current(
            session_id
        )

        self.current_session = session

        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        stored_messages = session.get(
            "messages",
            [],
        )

        for message in stored_messages:

            if not isinstance(
                message,
                dict,
            ):

                continue

            role = message.get("role")

            content = message.get(
                "content",
                "",
            )

            if role not in (
                "user",
                "assistant",
            ):

                continue

            if not isinstance(
                content,
                str,
            ):

                continue

            self.messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        return True

    def rename_conversation(
        self,
        session_id,
        title,
    ):

        success = (
            self.conversations.rename_session(
                session_id,
                title,
            )
        )

        if success:

            refreshed = (
                self.conversations.get_session(
                    session_id
                )
            )

            if (
                refreshed
                and self.current_session
                and self.current_session.get(
                    "id"
                )
                == session_id
            ):

                self.current_session = refreshed

        return success

    def delete_conversation(
        self,
        session_id,
    ):

        success = (
            self.conversations.delete_session(
                session_id
            )
        )

        if not success:

            return False

        if (
            self.current_session
            and self.current_session.get(
                "id"
            )
            == session_id
        ):

            self._restore_current_session()

        return True

    # ========================================================
    # Memory
    # ========================================================

    def get_memories(self):

        return self.memory.get_all()

    def add_memory(
        self,
        content,
    ):

        return self.memory.add_memory(
            content
        )

    def remove_memory(
        self,
        content,
    ):

        return self.memory.remove_memory(
            content
        )