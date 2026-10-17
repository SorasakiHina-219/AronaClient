
# core/computer_task.py
# AronaClient V0.2
# 电脑任务计划、用户授权与受控执行
#
# 第一阶段：
# 1. 验证操作计划
# 2. 用户确认后执行
# 3. 限制允许的操作类型
# 4. 支持取消和紧急停止
#
# 本模块不会执行 AI 生成的任意 Python 或系统命令。

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from threading import Event
from typing import Any


# ================================================================
# 白名单（第一阶段）
# ================================================================
#
# 只开放：
#   * 获取活动窗口信息   —— 只读
#   * 获取屏幕尺寸       —— 只读
#   * 获取屏幕截图       —— 只读（本地，不联网）
#   * 移动鼠标           —— 无副作用
#
# 明确**不**开放：点击 / 双击 / 右键 / 滚轮 / 键盘 / 快捷键 /
# 启动程序 / 文件操作 / 任意系统命令。
ALLOWED_ACTIONS = {
    "get_active_window",
    "get_screen_size",
    "capture_screen",
    "move_mouse",
}

# 只读、完全不碰用户输入的操作。
READ_ONLY_ACTIONS = {
    "get_active_window",
    "get_screen_size",
    "capture_screen",
}

MAX_STEPS = 10
MAX_COORDINATE = 10000
MAX_SCROLL_AMOUNT = 10

# 单个计划的整体执行超时（秒）。
EXECUTION_TIMEOUT = 30.0

# 批准凭据的有效期（秒）。自签发时刻起算。
TICKET_TTL = 120.0

# 已消费 nonce 的记录上限。
#
# 达到上限时【拒绝消费新凭据】，而不是淘汰仍有效的记录 ——
# 淘汰会让一个还在有效期内的 nonce 被遗忘，从而允许重放。
NONCE_CAPACITY = 4096

# 鼠标移动的时长上限，避免用超长动画拖住执行线程。
MAX_MOVE_DURATION = 3.0


def _note_lock_release_failure(where, exc):
    """
    记录「执行锁释放失败」。

    为什么需要这个函数：
        release() 在未持锁时会抛 RuntimeError("release unlocked lock") ——
        这正是「锁所有权被破坏」的强信号（第二轮审计 H-A 即属此类）。
        旧写法 `except Exception: pass` 会把它完全吞掉，
        使这类缺陷无法被发现。

    为什么仍然不向外抛：
        这三处调用都在 finally / 回滚路径上。
        若在此处抛异常，会**替换掉正在传播的原始异常**，
        掩盖真正的失败原因。因此只留痕，不改变控制流。

    为什么还要包一层 try：
        日志本身绝不能成为新的失败点。
    """

    try:
        print(
            f"[电脑任务] {where}: 执行锁释放失败，"
            f"所有权可能已被破坏：{exc!r}"
        )
    except Exception:
        pass


class ComputerTaskError(Exception):
    """电脑任务执行错误。"""


class PlanValidationError(ComputerTaskError):
    """操作计划不符合安全规则。"""


class TaskCancelledError(ComputerTaskError):
    """任务已取消或停止。"""


@dataclass
class ComputerAction:
    """一项经过结构化描述的电脑操作。"""

    action: str
    parameters: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class ComputerTaskPlan:
    """一个需要用户整体确认的电脑任务计划。"""

    title: str
    goal: str
    actions: list[ComputerAction]
    approved: bool = False

    # 计划唯一 ID。由 create_plan 生成，用于把批准凭据绑定到这一份计划。
    plan_id: str = ""

    # 批准时使用的凭据 nonce。
    # execute_plan 必须携带【同一张】凭据，否则拒绝。
    approved_ticket_nonce: str = ""

    # 批准时记录的计划指纹。
    #
    # 只要计划内容发生任何变化（增删步骤、改坐标、改参数），
    # 指纹就会变，旧批准立即失效。这样「批准 A 却执行了 B」不可能发生。
    approved_fingerprint: str = ""

    def fingerprint(self) -> str:
        """
        计算计划指纹。

        用「类型标记 + 规范化 JSON + SHA256」，而不是把字段用分隔符拼起来。

        这样有两个好处：
          * 不存在分隔符碰撞（旧实现里 title="a|b", goal="c"
            与 title="a", goal="b|c" 会算出同一个指纹）
          * 参数带上类型标记，1 与 "1"、1 与 1.0 不会被混为一谈

        任何一处改动（标题、目标、步骤顺序、动作名、参数、描述）
        都会让指纹变化，旧批准随之失效。
        """

        def norm(value):
            # 用类型名 + repr，保证既无歧义又能被 JSON 安全序列化
            return f"{type(value).__name__}:{value!r}"

        payload = {
            "title": self.title,
            "goal": self.goal,
            "actions": [
                {
                    "action": action.action,
                    "description": action.description,
                    "parameters": {
                        str(k): norm(v)
                        for k, v in sorted(
                            (action.parameters or {}).items()
                        )
                    },
                }
                for action in self.actions
            ],
        }

        blob = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        return hashlib.sha256(
            blob.encode("utf-8")
        ).hexdigest()


def actions_snapshot(actions):
    """
    把动作列表压成可比较的、含完整参数的快照。

    审批界面展示给用户的是这个快照；执行前再算一次并逐项比对，
    因此「用户同意的是 (100,200)，实际执行到 (9000,9000)」不可能发生。
    """

    out = []

    for action in actions:

        params = tuple(
            sorted(
                (str(k), repr(v))
                for k, v in (action.parameters or {}).items()
            )
        )

        out.append((action.action, params))

    return tuple(out)


@dataclass(frozen=True)
class ApprovalTicket:
    """
    一次性执行凭据。

    ⚠️ 保护范围必须如实理解：

    这是**显式调用标志**，不是不可伪造的用户身份凭据。
    任何能执行 Python 的代码都可以自己构造一张 ApprovalTicket。
    它的价值在于：

      * 把「批准」从一次普通函数调用，变成一次带 UI 上下文的显式对象传递
      * 让「自我批准」在代码审查里一眼可见（必须构造并传入 ticket）
      * 配合 nonce 消费，杜绝同一份批准被重复使用

    它**不能**防御同进程任意 Python 代码。
    """

    plan_id: str
    fingerprint: str
    actions: tuple
    issued_at: float
    nonce: str

    def is_expired(self, now=None):
        moment = time.monotonic() if now is None else float(now)
        return moment > (self.issued_at + TICKET_TTL)

    def remaining(self, now=None):
        moment = time.monotonic() if now is None else float(now)
        return max(0.0, self.issued_at + TICKET_TTL - moment)


@dataclass
class ComputerTaskResult:
    """电脑任务执行结果。"""

    success: bool
    message: str
    completed_steps: int = 0
    total_steps: int = 0
    stopped: bool = False
    timed_out: bool = False

    # 每一步的实际返回内容（只读操作会带信息回来）。
    outputs: list = field(default_factory=list)


def parse_plan_payload(payload):
    """
    把模型返回的内容严格解析成 (title, goal, actions)。

    只接受：
      * 已经是一个 dict
      * 或者是 JSON 文本

    **绝不使用 eval / exec**，也不接受任何可调用对象。
    任何格式问题都抛 PlanValidationError，绝不放行。
    """

    if isinstance(payload, (bytes, bytearray)):

        try:

            payload = payload.decode("utf-8")

        except Exception as exc:

            raise PlanValidationError(
                f"计划内容不是合法的 UTF-8 文本：{exc}"
            )

    if isinstance(payload, str):

        text = payload.strip()

        if not text:

            raise PlanValidationError("计划内容为空。")

        try:

            data = json.loads(text)

        except Exception as exc:

            raise PlanValidationError(
                f"计划不是合法 JSON：{exc}"
            )

    elif isinstance(payload, dict):

        data = payload

    else:

        raise PlanValidationError(
            "计划必须是 JSON 文本或字典。"
        )

    if not isinstance(data, dict):

        raise PlanValidationError("计划的顶层必须是 JSON 对象。")

    allowed_keys = {"title", "goal", "actions"}

    extra = set(data.keys()) - allowed_keys

    if extra:

        raise PlanValidationError(
            f"计划包含不认识的字段：{sorted(extra)}"
        )

    actions = data.get("actions")

    if not isinstance(actions, list):

        raise PlanValidationError("计划的 actions 必须是列表。")

    cleaned = []

    for index, item in enumerate(actions, start=1):

        if not isinstance(item, dict):

            raise PlanValidationError(
                f"第 {index} 步必须是 JSON 对象。"
            )

        item_extra = set(item.keys()) - {
            "action",
            "parameters",
            "description",
        }

        if item_extra:

            raise PlanValidationError(
                f"第 {index} 步包含不认识的字段：{sorted(item_extra)}"
            )

        params = item.get("parameters", {})

        if params is None:

            params = {}

        if not isinstance(params, dict):

            raise PlanValidationError(
                f"第 {index} 步的 parameters 必须是对象。"
            )

        cleaned.append(
            ComputerAction(
                action=item.get("action", ""),
                parameters=params,
                description=item.get("description", ""),
            )
        )

    return (
        data.get("title", ""),
        data.get("goal", ""),
        cleaned,
    )


class ComputerTaskManager:
    """
    管理电脑操作计划和受控执行。

    注意：
    - 创建计划不会操作电脑。
    - 未确认的计划不能执行。
    - 每个计划最多包含 MAX_STEPS 项操作。
    - 不接受任意 Python 代码或系统命令。
    """

    def __init__(self, ai_client, timeout: float = EXECUTION_TIMEOUT):
        self.ai_client = ai_client
        self._stop_event = Event()
        self._active_plan = None

        # 执行重入保护：
        # 同一时刻只允许一个计划处于执行中，避免并发执行产生重复授权。
        self._exec_lock = threading.Lock()
        self._executing = False

        # 已消费的 nonce：{nonce: 该凭据的到期时刻}
        #
        # 由 _ticket_lock 保护。锁顺序固定为
        #     _ticket_lock  ->  _exec_lock
        # 任何地方都不得反向获取，否则可能死锁。
        self._consumed_nonces = {}
        self._ticket_lock = threading.Lock()

        # 整体执行超时（秒）。
        self.timeout = float(timeout)

        # 把停止信号接到控制器上：
        # 一旦停止，控制器会在下一个操作前拒绝执行，
        # 而不是等整个计划跑完。
        try:

            self.ai_client._bind_stop_signal(
                self._stop_event.is_set
            )

        except Exception:

            pass

    @property
    def active_plan(self):
        return self._active_plan

    def create_plan(self, title, goal, actions):
        """
        创建并验证计划，但不执行任何操作。

        任务执行期间**拒绝**创建新计划：

            _active_plan 是「当前唯一有效的计划」。
            如果在 A 执行期间创建 B，B 会顶替 _active_plan，
            使 A 在下一步 validate_plan 时失配而中止 ——
            而 A 已经执行过的步骤无法回滚（部分执行）。

        这里用 _exec_lock 的非阻塞获取把「检查执行状态 + 写入 _active_plan」
        变成**原子**操作：拿到锁说明没有任务在执行，可以安全创建；
        拿不到锁说明有任务在执行，立即拒绝且不改动任何状态。

        锁顺序说明：本方法**只取 _exec_lock**，不嵌套 _ticket_lock，
        与既有的 _ticket_lock -> _exec_lock 约定一致，不构成环。

        actions 示例：
        [
            ComputerAction(
                action="move_mouse",
                parameters={"x": 500, "y": 300},
                description="移动鼠标到目标位置",
            )
        ]
        """

        # 只有成功取得锁的调用才允许释放它（见下方 try/finally）。
        if not self._exec_lock.acquire(blocking=False):

            raise ComputerTaskError(
                "有任务正在执行，暂时不能创建新的电脑操作计划。"
            )

        try:

            return self._create_plan_locked(title, goal, actions)

        finally:

            try:
                self._exec_lock.release()
            except Exception as exc:
                _note_lock_release_failure("create_plan", exc)

    def _create_plan_locked(self, title, goal, actions):
        """
        create_plan 的实际实现。

        调用方**必须**已经持有 _exec_lock；本方法自身不获取也不释放锁。
        拆成独立方法是为了让 create_plan 的 try/finally 结构保持清晰，
        避免把原有的校验与构造逻辑整段缩进、扩大差异。
        """

        if not isinstance(title, str) or not title.strip():
            raise PlanValidationError("计划标题不能为空。")

        if not isinstance(goal, str) or not goal.strip():
            raise PlanValidationError("任务目标不能为空。")

        if not isinstance(actions, list) or not actions:
            raise PlanValidationError("操作计划不能为空。")

        if len(actions) > MAX_STEPS:
            raise PlanValidationError(
                f"单个计划最多允许 {MAX_STEPS} 个操作步骤。"
            )

        normalized_actions = []

        for index, action in enumerate(actions, start=1):
            if isinstance(action, ComputerAction):
                item = action
            elif isinstance(action, dict):
                item = ComputerAction(
                    action=action.get("action", ""),
                    parameters=action.get("parameters", {}),
                    description=action.get("description", ""),
                )
            else:
                raise PlanValidationError(
                    f"第 {index} 步不是有效的操作描述。"
                )

            self._validate_action(item, index)
            normalized_actions.append(item)

        plan = ComputerTaskPlan(
            title=title.strip(),
            goal=goal.strip(),
            actions=normalized_actions,
            approved=False,
            plan_id=secrets.token_urlsafe(16),
        )

        self._active_plan = plan

        # 这里**不再**清除停止信号。
        #
        # 旧实现会 clear()，于是「A 执行中用户点了停止」之后
        # 只要有人创建一个新计划，A 的停止请求就被抹掉 ——
        # 两道保护（活动计划失配 + 停止信号）同时失效。
        #
        # 「新一轮执行前重置停止信号」的职责已由 approve_plan（批准新计划时）
        # 与 execute_plan（开始执行时）各自承担，删除这一行不会丢失功能。

        return plan

    def _validate_action(self, item, index):
        """验证单项操作的类型与参数。"""

        if item.action not in ALLOWED_ACTIONS:
            raise PlanValidationError(
                f"第 {index} 步包含不允许的操作：{item.action!r}"
            )

        if not isinstance(item.parameters, dict):
            raise PlanValidationError(
                f"第 {index} 步的参数必须是字典。"
            )

        p = item.parameters

        if not isinstance(p, dict):
            raise PlanValidationError(
                f"第 {index} 步的参数必须是字典。"
            )

        # ---------------- 只读操作：不允许任何参数 ----------------
        if item.action in READ_ONLY_ACTIONS:

            if p:

                raise PlanValidationError(
                    f"第 {index} 步是只读操作，不接受任何参数：{sorted(p)}"
                )

        # ---------------- 移动鼠标 ----------------
        elif item.action == "move_mouse":

            if "x" not in p or "y" not in p:
                raise PlanValidationError(
                    f"第 {index} 步缺少鼠标坐标。"
                )

            extra = set(p.keys()) - {"x", "y", "duration"}

            if extra:

                raise PlanValidationError(
                    f"第 {index} 步包含不接受的参数：{sorted(extra)}"
                )

            for name in ("x", "y"):

                value = p[name]

                # bool 是 int 的子类，必须单独排除，否则 True 会被当成 1。
                if isinstance(value, bool) or not isinstance(value, int):

                    raise PlanValidationError(
                        f"第 {index} 步的 {name} 必须是整数。"
                    )

                if not 0 <= value <= MAX_COORDINATE:

                    raise PlanValidationError(
                        f"第 {index} 步的 {name} 超出允许范围。"
                    )

            if "duration" in p:

                duration = p["duration"]

                if isinstance(duration, bool) or not isinstance(
                    duration, (int, float)
                ):

                    raise PlanValidationError(
                        f"第 {index} 步的 duration 必须是数字。"
                    )

                if not 0.0 <= float(duration) <= MAX_MOVE_DURATION:

                    raise PlanValidationError(
                        f"第 {index} 步的 duration 超出允许范围。"
                    )

        if not isinstance(item.description, str):
            raise PlanValidationError(
                f"第 {index} 步的描述必须是文本。"
            )

    def certify_plan(self, plan):
        """
        授权入口专用的「完整校验」。

        这是 AIClient 打开执行授权前唯一被认可的凭据来源。
        只有同时满足下面全部条件，才会返回本次允许执行的动作列表：

          1. 传入的确实是本管理器当前的活动计划
          2. 该计划已经获得用户批准
          3. 批准指纹与当前内容一致（参数被改过就作废）
          4. 每一个动作都在白名单内且参数合法
          5. 任务没有被停止
          6. 没有另一个计划正在执行（防重入 / 防并发）

        任何一条不满足都抛异常，绝不返回授权。
        """

        if plan is None:
            raise PlanValidationError("没有可授权的计划。")

        # 只接受真正的 ComputerTaskPlan；挡掉鸭子类型的伪造计划对象
        # （第三轮审计 M-1）。
        if not isinstance(plan, ComputerTaskPlan):
            raise PlanValidationError(
                "计划类型不合法，拒绝授权。"
            )

        if plan is not self._active_plan:
            raise PlanValidationError("计划不是当前有效计划。")

        if not plan.approved:
            raise PlanValidationError(
                "此计划尚未获得用户确认，不能授权执行。"
            )

        if plan.approved_fingerprint != plan.fingerprint():

            plan.approved = False
            plan.approved_fingerprint = ""

            raise PlanValidationError(
                "计划在批准之后被修改，原批准已失效。"
            )

        # 白名单 + 参数逐项重校验
        if not plan.actions or len(plan.actions) > MAX_STEPS:
            raise PlanValidationError("计划步骤数量不合法。")

        for index, action in enumerate(plan.actions, start=1):
            self._validate_action(action, index)

        if self._stop_event.is_set():
            raise TaskCancelledError("任务已被停止，不能授权执行。")

        if self._executing:
            raise TaskCancelledError(
                "已有任务正在执行，不能重复授权。"
            )

        return [action.action for action in plan.actions]

    # --------------------------------------------------------
    # 批准凭据
    # --------------------------------------------------------

    def _validate_ticket(self, plan, ticket):
        """
        校验凭据确实绑定到这一份计划。

        只做校验，不改任何状态；由调用方在 _ticket_lock 内使用。
        """

        if not isinstance(ticket, ApprovalTicket):

            raise PlanValidationError(
                "缺少有效的批准凭据。"
            )

        if not plan.plan_id or ticket.plan_id != plan.plan_id:

            raise PlanValidationError(
                "批准凭据与当前计划不是同一份。"
            )

        if ticket.fingerprint != plan.fingerprint():

            raise PlanValidationError(
                "计划内容与批准时不一致，凭据已失效。"
            )

        if tuple(ticket.actions) != actions_snapshot(plan.actions):

            raise PlanValidationError(
                "计划动作参数与批准时不一致，凭据已失效。"
            )

        if ticket.is_expired():

            raise PlanValidationError(
                f"批准凭据已过期（有效期 {TICKET_TTL:.0f} 秒）。"
            )

        if ticket.nonce in self._consumed_nonces:

            raise PlanValidationError(
                "该批准凭据已经被使用过，不能重复使用。"
            )

        return True

    def _consume_nonce(self, ticket):
        """
        消费凭据的 nonce。必须已在 _ticket_lock 内调用。

        记录管理策略：
          * 只清理【已过期】的记录 —— 过期的 nonce 不再可能被重放
          * 容量达到上限时【拒绝】，绝不淘汰仍有效的记录
        """

        now = time.monotonic()

        expired = [
            nonce
            for nonce, expires_at in self._consumed_nonces.items()
            if expires_at < now
        ]

        for nonce in expired:

            self._consumed_nonces.pop(nonce, None)

        if ticket.nonce in self._consumed_nonces:

            raise PlanValidationError(
                "该批准凭据已经被使用过，不能重复使用。"
            )

        if len(self._consumed_nonces) >= NONCE_CAPACITY:

            raise ComputerTaskError(
                f"凭据记录已达上限 {NONCE_CAPACITY}。"
                "为避免遗忘仍在有效期内的凭据，拒绝消费新的批准凭据。"
            )

        self._consumed_nonces[ticket.nonce] = ticket.issued_at + TICKET_TTL

        return True

    def _redeem_and_begin(self, plan, ticket, timeout):
        """
        在同一临界区内完成：校验凭据 -> 取得执行权 -> 建立授权 -> 消费 nonce。

        这样设计是为了满足两点：

        1. **防并发重放**：校验与消费之间没有空隙，两个线程不可能同时通过。
        2. **不留半成品**：任何一步失败，都把已经拿到的执行权与授权还回去。

        锁顺序：本方法持 _ticket_lock 后再取 _exec_lock（非阻塞），
        与全局约定的 _ticket_lock -> _exec_lock 一致，不会死锁。

        所有权语义（第二轮审计 H-A）：
            * 取得 _exec_lock **失败**时直接抛出，**不释放任何锁** ——
              调用方必须据此知道「本次调用没有取得执行权」。
            * 取得锁之后若建立授权或消费 nonce 失败，
              在本方法的 except 分支里释放**自己取得的**那把锁并回滚授权。
            * 只有成功返回，才代表本调用持有 _exec_lock。

        调用方（execute_plan）必须用**局部变量**记录是否成功返回，
        并只在该变量为真时才释放锁与清理全局状态。
        """

        with self._ticket_lock:

            # ① 校验（不改状态）
            self._validate_ticket(plan, ticket)

            # ② 取得执行权（非阻塞；拿不到就说明已有任务在跑）
            if not self._exec_lock.acquire(blocking=False):

                raise ComputerTaskError(
                    "已有任务正在执行，拒绝并发执行。"
                )

            try:

                # ③ 建立授权（可能因类型/白名单/停止状态而失败）
                started_at = self.ai_client._begin_execution(
                    plan,
                    timeout=timeout,
                )

                # ④ 消费 nonce（可能因重复/容量上限而失败）
                self._consume_nonce(ticket)

            except Exception:

                # 回滚：还授权、还执行权，nonce 不动 —— 凭据没有被花掉
                try:
                    self.ai_client._end_execution()
                except Exception:
                    pass

                try:
                    self._exec_lock.release()
                except Exception as exc:
                    _note_lock_release_failure("_redeem_and_begin 回滚", exc)

                raise

            self._executing = True

            return started_at

    # --------------------------------------------------------
    # 计划审批与执行
    # --------------------------------------------------------

    def approve_plan(self, plan, ticket):
        """
        记录对指定计划的确认。

        ⚠️ 必须如实看待这里的保证边界：

        本方法**无法验证「真的是用户在界面上点了同意」**。
        它只确认「这是一份合法计划，且调用方声明要批准它」。
        任何能执行 Python 的代码都可以调用它完成自我批准。

        因此它是**纪律性约束，不是安全隔离**。
        真正的用户确认必须由界面层负责：审批对话框应当是
        approve_plan 在实际运行中唯一的调用者。
        （详见第三轮审计 H-1）
        """

        if plan is not self._active_plan:
            raise PlanValidationError(
                "计划已失效，请重新生成并确认。"
            )

        self.validate_plan(plan)

        # 批准必须携带一张与本计划完全一致的凭据。
        # 这一步不使用 _ticket_lock：此时并不消费 nonce，
        # 只是校验凭据与计划绑定（消费发生在 _redeem_and_begin）。
        self._validate_ticket(plan, ticket)

        # 把批准与这一份具体计划、以及这一张凭据绑定。
        plan.approved_fingerprint = plan.fingerprint()
        plan.approved_ticket_nonce = ticket.nonce
        plan.approved = True

        self._stop_event.clear()

    def cancel_plan(self, plan=None):
        """取消待执行计划。"""

        target = plan or self._active_plan

        if target is not None:
            target.approved = False

        if target is self._active_plan:
            self._active_plan = None

        self._stop_event.set()

    def request_stop(self):
        """请求停止当前执行流程。"""

        self._stop_event.set()

        if self._active_plan is not None:
            self._active_plan.approved = False

    def validate_plan(self, plan):
        """执行前重新验证计划，避免使用过期授权。"""

        if plan is not self._active_plan:
            raise PlanValidationError("计划不是当前有效计划。")

        if not plan.actions or len(plan.actions) > MAX_STEPS:
            raise PlanValidationError("计划步骤数量不合法。")

        # 批准之后只要计划内容被改过，指纹就对不上，旧批准立即失效。
        if plan.approved:

            if plan.approved_fingerprint != plan.fingerprint():

                plan.approved = False
                plan.approved_fingerprint = ""

                raise PlanValidationError(
                    "计划在批准之后被修改，原批准已失效，请重新确认。"
                )

        for index, action in enumerate(plan.actions, start=1):
            self._validate_action(action, index)

    def execute_plan(self, plan, ticket):
        """
        执行已经确认的计划。

        必须携带**与批准时同一张**凭据（nonce 一致）。
        凭据的校验、nonce 消费与执行权取得在 _redeem_and_begin 内原子完成。

        应当从后台线程调用，不能直接在 UI 主线程运行。
        """

        if plan is not self._active_plan:
            raise PlanValidationError("计划已失效。")

        if not plan.approved:
            raise PlanValidationError(
                "此计划尚未获得用户确认，不能执行。"
            )

        if not isinstance(ticket, ApprovalTicket) or (
            ticket.nonce != plan.approved_ticket_nonce
        ):
            raise PlanValidationError(
                "执行时必须携带批准该计划时所用的同一张凭据。"
            )

        self.validate_plan(plan)

        if not self.ai_client.is_computer_control_enabled():
            raise ComputerTaskError(
                "电脑控制权限未开启，未执行任何操作。"
            )

        completed = 0
        total = len(plan.actions)
        outputs = []
        timed_out = False

        # 本次调用是否真正取得了执行权。
        #
        # 这是**局部**变量，不是共享布尔量：每个调用各有一份，
        # 因此 B 线程失败时不可能影响 A 线程的所有权判断。
        # 只有它为 True 时才允许释放 _exec_lock、清理 _executing、
        # 关闭执行授权或改动活动计划。
        lock_owned = False

        try:
            self._stop_event.clear()

            # 凭据校验 + nonce 消费 + 执行权取得 + 建立授权，
            # 全部在 _redeem_and_begin 的同一临界区内完成：
            # 要么全部成功，要么全部回滚，不留半成品。
            #
            # 只有这一行**成功返回**，本调用才真正持有 _exec_lock。
            started_at = self._redeem_and_begin(
                plan,
                ticket,
                timeout=self.timeout,
            )

            lock_owned = True

            # 超时时钟从「计划开始执行」这一刻起算，
            # 任务超时 self.timeout 与授权超时使用同一个值（修复 M2）。
            deadline = started_at + self.timeout

            try:
                for action in plan.actions:

                    if self._stop_event.is_set():
                        raise TaskCancelledError("任务已停止。")

                    if time.monotonic() > deadline:
                        timed_out = True
                        raise TaskCancelledError("任务执行超时。")

                    # 执行每一步之前再次检查授权与计划指纹。
                    if not plan.approved:
                        raise TaskCancelledError("任务授权已撤销。")

                    self.validate_plan(plan)

                    outputs.append(
                        self._execute_action(action)
                    )

                    completed += 1

                return ComputerTaskResult(
                    success=True,
                    message="计划中的操作已执行完毕，需要进一步验证任务结果。",
                    completed_steps=completed,
                    total_steps=total,
                    outputs=outputs,
                )

            except Exception as exc:

                return ComputerTaskResult(
                    success=False,
                    message=f"任务已停止：{exc}",
                    completed_steps=completed,
                    total_steps=total,
                    stopped=True,
                    timed_out=timed_out,
                    outputs=outputs,
                )

        finally:

            # ---------- 只撤销【本份计划】的批准状态 ----------
            #
            # 这是计划自身的状态，任何路径（包括未取得执行权的调用）都应清理。
            plan.approved = False
            plan.approved_fingerprint = ""

            # ---------- 全局状态只有「锁的所有者」才能清理 ----------
            #
            # 取得执行权失败的调用若去释放 _exec_lock，
            # 会把正在执行的另一个线程的锁释放掉，
            # 从而让第三个调用并发进入（第二轮审计 H-A）。
            #
            # 同理，它也不得关闭别人的执行授权、复位别人的 _executing。
            if lock_owned:

                try:
                    self.ai_client._end_execution()
                except Exception:
                    pass

                try:
                    self.ai_client.release_all_inputs()
                except Exception:
                    pass

                self._executing = False

                try:
                    self._exec_lock.release()
                except Exception as exc:
                    _note_lock_release_failure("execute_plan 收尾", exc)

                if self._active_plan is plan:
                    self._active_plan = None

            # 未取得执行权：只留下最小的本地痕迹，不碰任何全局状态。
            elif self._active_plan is plan:

                self._active_plan = None

    def _execute_action(self, item):
        """
        将白名单动作映射到 AIClient 的受控接口。

        这里只做「动作名 -> 方法」的映射；
        真正的权限校验在 AIClient 内部统一完成，两边各拦一道。
        """

        p = item.parameters

        if item.action == "get_active_window":

            return self.ai_client.get_active_window_info()

        if item.action == "get_screen_size":

            return self.ai_client.get_screen_size()

        if item.action == "capture_screen":

            return self.ai_client.get_desktop_screenshot()

        if item.action == "move_mouse":

            return self.ai_client.move_mouse(
                x=p["x"],
                y=p["y"],
                duration=float(p.get("duration", 0.2)),
            )

        # 理论上到不了这里：_validate_action 已经拦过一遍白名单。
        raise PlanValidationError(
            f"没有可执行的动作映射：{item.action!r}"
        )