# agent/launcher.py
# AronaClient A1' —— 主进程侧的 agent 会话
#
# ============================================================
# 职责
# ============================================================
#
#   * 以【挂起】方式创建 agent 进程（见 winjob）
#   * 绑定 Job Object【成功之后】才发送第一条应用层消息
#   * 提供 ping / bye 的请求-应答
#   * 处理：启动失败、绑定失败、管道断开、帧非法、
#           心跳超时、agent 崩溃、优雅停止超时
#
# ============================================================
# 硬性顺序（不可调换）
# ============================================================
#
#   spawn_suspended_in_job()   <- 已包含 创建/绑定/恢复
#        │
#        ├─ 失败 -> 抛异常，【不发送任何消息】
#        ▼
#   只有到这里才允许 send()
#
# 本模块不 import pyautogui，也不加载任何控制器。

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent.authchannel import AuthChannel, AuthError
from agent.crypto import action_id_for, derive_keys, params_hash
from agent.protocol import (
    FrameError,
    MAX_PAYLOAD,
    PROTOCOL_VERSION,
    MSG_BYE,
    MSG_BYE_ACK,
    MSG_HELLO,
    MSG_PING,
    MSG_PONG,
    MSG_PROBE,
    MSG_PROBE_ACK,
    MSG_READY,
    decode_frame_from_buffer,
    encode_frame,
    make_message,
)
from agent.winjob import SpawnedProcess, spawn_suspended_in_job

__all__ = [
    "AgentLaunchError",
    "AgentSession",
    "SessionState",
    "start_agent",
]

# 默认超时（秒）
DEFAULT_HANDSHAKE_TIMEOUT = 10.0
DEFAULT_PING_TIMEOUT = 5.0
DEFAULT_SHUTDOWN_GRACE = 5.0
DEFAULT_PROBE_TIMEOUT = 5.0

# 读取队列容量（块数）。8192 字节/块 -> 约 4 MiB 上限。
# 有界是必需项，见 _start_reader 的说明。
QUEUE_MAX_CHUNKS = 512

# stderr 最多保留的字节数（仅诊断用）
STDERR_MAX_BYTES = 256 * 1024

# ---- B-3：超时/接收原因的显式常量 ----
#
# 区分三类超时语义：
#   REASON_TIMEOUT —— **轮询超时**：本次调用期间没有数据。
#                     正常现象，**不改变会话状态**。
#   （事务超时）    —— 由 `_recv_transactional()` 产生：期望某条应答却超时，
#                     判为故障并置 FAULTED。
#   （握手超时）    —— 由 `wait_ready()` 走事务路径处理。
REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_EOF = "eof"
REASON_DEAD = "dead"
REASON_OVERFLOW = "overflow"


class AgentLaunchError(Exception):
    """agent 启动或绑定失败。"""


class SessionState:
    """会话状态（A1' 的最小状态机）。"""

    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULTED = "FAULTED"

    # ---- ARONASEC-NIGHT-002 修复 2：生命周期三态（L-3b）----
    #
    #   OPEN    —— 正常可用
    #   CLOSING —— 首轮清理进行中（并发 close 必须等待）
    #   CLOSED  —— 清理流程已结束
    #
    # ⚠️ CLOSED 只表示【清理流程结束】，不代表所有资源都成功关闭；
    #    资源关闭失败仍通过 cleanup_errors 如实报告。
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


@dataclass
class AgentSession:
    """
    一次 agent 会话。

    典型用法：

        with AgentSession(project_root) as sess:
            sess.wait_ready()
            assert sess.ping()
            sess.stop()
    """

    project_root: Path
    python_exe: str | None = None
    idle_timeout: float = 30.0

    # 运行期状态（外部只读）
    state: str = SessionState.STARTING
    session_id: str = ""
    pid: int = 0
    last_error: str = ""

    _proc: SpawnedProcess | None = field(default=None, repr=False)
    _buffer: bytearray = field(default_factory=bytearray, repr=False)
    _messages_from_agent: int = 0

    # ---- A2'-I：认证状态 ----
    # key 帧只允许发送一次；用显式标志强制（AuthChannel 本身不做该约束）
    _key_sent: bool = field(default=False, repr=False)
    channel: AuthChannel | None = field(default=None, repr=False)
    _keys: object | None = field(default=None, repr=False)
    # 初始化阶段标志：握手完成前不接受业务应答
    handshake_done: bool = False

    # ---- A2'-II（B-1）：生命周期同步 ----
    #
    # _life_lock 只保护【状态与引用的一致性】，**绝不覆盖 I/O**：
    #   * 持锁期间只做判断、取引用、seal/open（纯 CPU）
    #   * 管道读写、terminate、join 一律在锁外执行
    # 这样既不会有"检查后使用"的竞态，也不会因为持锁做 I/O 而死锁。
    _life_lock: object = field(default_factory=threading.RLock, repr=False)
    # 关闭已开始：此后拒绝新的 send/recv 业务操作
    _closing: bool = False

    # ---- ARONASEC-NIGHT-002 修复 2：并发 close 三态 + L-3b 等待 ----
    #
    # lifecycle: OPEN -> CLOSING -> CLOSED
    #   * 首个 close() 原子地把 OPEN 置为 CLOSING 并执行唯一一轮清理
    #   * 并发 close() 发现已是 CLOSING/CLOSED -> 等待首轮完成
    #   * 等待者【不重复执行清理】，只等待同一轮结果
    #
    # _close_done 用于唤醒等待者；即使清理自身抛异常，
    # 也必须在 finally 中 set()，避免永久等待。
    lifecycle: str = "OPEN"
    _close_done: object = field(default_factory=threading.Event, repr=False)
    _close_owner: bool = False

    # ---- ARONASEC-NIGHT-002 修复 1：P-1 发送失败契约 ----
    #
    # 三个必须区分的事实：
    #   ① 序号已分配  —— seal() 已推进 _out_seq，不能声称没推进
    #   ② 发送未成功提交 —— 未确认完整帧写入，不能记作发送成功
    #   ③ 会话不可继续发送 —— 写入失败后禁止后续帧
    #
    # _send_failed 一旦置位即不再复位（只在本会话生命周期内有效）。
    _send_failed: bool = False
    # 最近一次发送失败的结构化原因（供诊断，不含密钥/明文）
    _send_failure_reason: str = ""

    # ---- A2'-II（B-5）：清理错误可观测 ----
    # 每一项清理失败都会追加到这里，调用方可检查
    cleanup_errors: list = field(default_factory=list)
    # teardown 是否已完成（幂等判据）
    teardown_done: bool = False

    # -----------------------------------------------------------
    # 启动
    # -----------------------------------------------------------

    def start(self) -> None:
        """
        创建 agent 进程。

        **本方法成功返回之前，不会发出任何应用层消息。**
        这是 A1' 的硬性验收条件之一。
        """

        root = Path(self.project_root).resolve()
        python = self.python_exe or sys.executable
        self.session_id = uuid.uuid4().hex[:16]

        argv = [python, "-m", "agent"]

        try:
            # spawn_suspended_in_job 内部：
            #   CreateJobObject -> CreatePipe -> CreateProcessW(SUSPENDED)
            #   -> AssignProcessToJobObject -> ResumeThread
            # 其中任何一步失败都会回滚并抛异常。
            self._proc = spawn_suspended_in_job(
                argv,
                cwd=str(root),
                env={
                    "ARONA_AGENT_SESSION": self.session_id,
                    "ARONA_AGENT_IDLE_TIMEOUT": str(self.idle_timeout),
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            )

        except Exception as exc:
            self.state = SessionState.FAULTED
            self.last_error = f"启动/绑定失败：{type(exc).__name__}: {exc}"
            raise AgentLaunchError(self.last_error) from None

        self.pid = self._proc.pid
        self.state = SessionState.STARTING

        # 读取线程必须在任何 _recv 之前启动
        self._start_reader()

        # ---- A2'-I：建立认证通道并发送一次性密钥帧 ----
        #
        # 顺序：Job 绑定已成功（spawn_suspended_in_job 内部完成）
        #       -> 才发送 key 帧 -> 才允许任何业务消息
        self._keys = derive_keys()
        self.channel = AuthChannel(self._keys, self.session_id, role="master")

        self._send_key_frame()

    def _send_key_frame(self) -> None:
        """
        发送自举密钥帧。**只允许一次**。

        为什么需要显式标志：
            AuthChannel 只保证"会话中拒收 key 帧"，
            但 `seal()` 在任何时刻都愿意发出 key 帧。
            "只发一次"必须由本层强制，否则重复初始化会替换密钥。
        """

        if self._key_sent:
            self._fail("密钥帧已发送过，拒绝重复初始化")
            raise AgentLaunchError(self.last_error)

        if self.channel is None:
            self._fail("认证通道未建立")
            raise AgentLaunchError(self.last_error)

        # ★ A2'-II 裁定 PC-2：改由 L0 初始化专用入口构造未签名密钥帧，
        #   不再在 launcher 内联拼接（那样会绕开通道的职责边界）。
        frame = self.channel.build_key_frame(
            self.session_id, self._keys.to_frame_fields()
        )

        # 明文、无签名 —— 自举阶段双方尚无共享密钥（见 crypto 模块头部说明）
        os.write(self._proc.stdin_write, encode_frame(frame))
        self._key_sent = True

    # -----------------------------------------------------------
    # 收 / 发
    # -----------------------------------------------------------

    def send(self, message: dict) -> None:
        """
        发一帧（自动经认证通道封装）。

        入参是【未封装】的业务消息，例如 {"type": "ping", "probe_id": "p"}。
        本方法负责加 seq / msg_nonce / session_id / tag。

        ★ B-1：与 close() 的并发由 _life_lock 协调。
          持锁期间只做「检查 + seal」（纯 CPU，不会阻塞），
          真正的 os.write 在锁外执行 —— 避免持锁做 I/O 造成死锁。

          若在释放锁之后 close() 关掉了 fd，os.write 会抛 OSError，
          该错误被**可观测地**记录（不会静默忽略）。
          这满足"不得仅捕获 AttributeError 后忽略"的要求。
        """

        # ---- 锁内：一致性检查 + 封装（无 I/O）----
        with self._life_lock:

            if self._proc is None:
                raise AgentLaunchError("会话尚未启动或已关闭。")

            if self._closing:
                raise AgentLaunchError("会话正在关闭，拒绝发送。")

            # ★ 修复 1（P-1）：发送失败后禁止后续发送
            if self._send_failed:
                raise AgentLaunchError(
                    f"会话已因发送失败进入故障态，不可继续发送："
                    f"{self._send_failure_reason}"
                )

            channel = self.channel

            if channel is None:
                self._fail("认证通道不可用（未建立或已 teardown）")
                raise AgentLaunchError(self.last_error)

            try:
                sealed = channel.seal(message)

            except AuthError as exc:
                self._fail(f"封装失败：{exc}")
                raise AgentLaunchError(self.last_error) from None

            fd = self._proc.stdin_write

        # ---- 锁外：I/O ----
        #
        # ★ ARONASEC-NIGHT-002 修复 1（P-1）：
        #   ① 必须检查 os.write 的【返回值】—— 短写不得当作成功
        #   ② 失败后【不回滚】已分配的序号（回滚会造成状态重用）
        #   ③ 失败后置 _send_failed，禁止后续发送
        #
        #   注意：序号在 seal() 内已分配（事实①）。
        #        本次调用若失败，只是"未成功提交"（事实②），
        #        并立即把会话标记为不可继续发送（事实③）。
        try:
            payload = encode_frame(sealed)

        except FrameError:
            raise

        try:
            written = os.write(fd, payload)

        except OSError as exc:
            self._note_send_failure(
                f"写入失败：{type(exc).__name__}: {exc}", sealed
            )
            raise AgentLaunchError(self.last_error) from None

        # 短写检测：一次 write 未写完整帧 -> 协议边界已损坏
        if written is None or written != len(payload):
            self._note_send_failure(
                f"短写：期望 {len(payload)} 字节，实际 "
                f"{'未知' if written is None else written} 字节",
                sealed,
            )
            raise AgentLaunchError(self.last_error) from None

    def _start_reader(self) -> None:
        """
        启动后台读取线程。

        为什么不用 select：
            本机实测 —— Windows 上 select.select() 只支持 socket，
            对管道句柄会抛
                OSError: [WinError 10093] 应用程序没有调用 WSAStartup…
            CPython 的 subprocess 也是用读取线程解决的，这里照做。

        线程把 stdout 的字节块塞进队列；读到 EOF 或出错则放一个哨兵。

        为什么队列必须有界（A1' 源码复核发现）：
            帧上界只约束【单帧】，不约束【积压总量】。
            无界队列在 agent 持续输出时会让主进程内存不断增长。
            这里设容量上限，超出即标记溢出并停止读取。
        """

        import queue
        import threading

        self._queue = queue.Queue(maxsize=QUEUE_MAX_CHUNKS)
        self._stderr_chunks = []
        self._eof = False
        self._overflow = False

        def _read_stdout():
            while True:
                try:
                    chunk = os.read(self._proc.stdout_read, 8192)
                except OSError:
                    break
                if not chunk:
                    break

                try:
                    self._queue.put(chunk, timeout=1.0)
                except queue.Full:
                    # 对端在灌数据 —— 停止读取并标记溢出
                    self._overflow = True
                    break

            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

        def _read_stderr():
            # stderr 只用于诊断，同样不能无界增长
            total = 0
            while True:
                try:
                    chunk = os.read(self._proc.stderr_read, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                if total >= STDERR_MAX_BYTES:
                    break
                self._stderr_chunks.append(chunk)
                total += len(chunk)

        self._reader_out = threading.Thread(
            target=_read_stdout, name="agent-stdout", daemon=True)
        self._reader_err = threading.Thread(
            target=_read_stderr, name="agent-stderr", daemon=True)
        self._reader_out.start()
        self._reader_err.start()

    def join_readers(self, timeout: float = 2.0) -> bool:
        """
        等待读取线程退出。

        A1' 源码复核发现：close() 直接用 os.close(fd) 关闭读端，
        而读取线程可能正阻塞在 os.read(fd) 上。CPython 未保证这种
        竞争下 fd 不会被复用给其他文件。

        正确顺序：先终止进程（触发 EOF）-> join 线程 -> 再关 fd。

        超时策略（A1' 复核要求）：
            每个线程最多等 timeout 秒，**方法本身因此有界**
            （最坏 2 × timeout）。
            若超时仍有线程存活，返回 False，并把会话标记为 FAULTED ——
            调用方必须据此停止使用该会话，不得继续读它。

            残留线程是 daemon 线程，会随进程退出而消失；
            但在本进程存活期间，它们可能仍持有已关闭的 fd 号。
            因此失败时必须让会话进入不可用状态，而不是"再试一次"。
        """

        ok = True

        for t in (getattr(self, "_reader_out", None),
                  getattr(self, "_reader_err", None)):

            if t is None or not t.is_alive():
                continue

            t.join(timeout=timeout)

            if t.is_alive():
                ok = False

        if not ok:
            self._fail(
                "读取线程在超时内未退出（子进程可能无法终止，"
                "或管道未产生 EOF）—— 会话不可继续使用"
            )

        return ok

    def stderr_text(self) -> str:
        """agent 的 stderr（用于诊断，不参与协议）。"""

        return b"".join(self._stderr_chunks).decode("utf-8", "replace")

    def _recv(self, timeout: float):
        """
        读一帧，并**无条件执行认证校验**。

        返回 (message | None, reason)。
        reason ∈ {REASON_OK, REASON_TIMEOUT, REASON_EOF, REASON_DEAD,
                  REASON_OVERFLOW, "error:…", "auth:…"}

        ★ B-2：**不存在跳过认证的参数**。
          旧版有 `authenticate=False` 逃生口（A2'-I 审计 B-2），
          已移除 —— 普通调用方无法关闭认证检查。
          自举密钥帧不走本函数，它由 start() 内的初始化流程单独处理，
          且只在建立通道之前发送。

        ★ B-3：`REASON_TIMEOUT` 只表示**这一次轮询没有数据**，
          不改变会话状态 —— 短时轮询超时是预期的。
          事务性等待请用 `_recv_transactional()`，它会把超时判为故障。
        """

        if self._proc is None:
            return None, REASON_DEAD

        import queue as _queue

        deadline = time.monotonic() + timeout

        while True:

            # ★ 队列溢出必须成为【确定的失败】，而不是让它退化成超时。
            if getattr(self, "_overflow", False):
                self._fail("读取队列溢出：agent 输出速率超出上限，"
                           "已停止读取以避免内存无界增长")
                return None, REASON_OVERFLOW

            # 先看缓冲区里有没有完整帧
            try:
                message, consumed = decode_frame_from_buffer(self._buffer)
            except FrameError as exc:
                self._fail(f"帧格式非法：{exc}")
                return None, f"error:{exc}"

            if message is not None:
                del self._buffer[:consumed]
                self._messages_from_agent += 1

                # ---- 统一认证：无条件执行，无逃生口 ----
                with self._life_lock:
                    channel = self.channel

                if channel is None:
                    self._fail("认证通道不可用（已关闭或未建立），拒绝处理消息")
                    return None, "auth:no channel"

                ok, why = channel.open(message)

                if not ok:
                    self._fail(f"入站消息认证失败：{why}")
                    return None, f"auth:{why}"

                return message, REASON_OK

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, REASON_TIMEOUT

            try:
                chunk = self._queue.get(timeout=min(remaining, 0.2))

            except _queue.Empty:
                # 没有新数据；顺便检查进程是否已死
                if not self._proc.is_alive():
                    self._fail_if_established("agent 进程已退出（读队列空闲）")
                    return None, "dead"
                continue

            if chunk is None:
                # EOF 哨兵
                self._eof = True
                if not self._proc.is_alive():
                    self._fail_if_established("agent 进程已退出（EOF）")
                    return None, "dead"
                return None, "eof"

            self._buffer.extend(chunk)

    # -----------------------------------------------------------
    # 会话操作（全部无副作用）
    # -----------------------------------------------------------

    def wait_ready(self, timeout: float = DEFAULT_HANDSHAKE_TIMEOUT) -> bool:
        """
        等待 agent 的 hello，并完成版本协商。

        返回 True 表示握手成功。
        """

        message, reason = self._recv(timeout)

        if message is None:
            self._fail(f"等待 hello 失败：{reason}")
            return False

        if message.get("type") != MSG_HELLO:
            self._fail(f"期望 hello，收到 {message.get('type')!r}")
            return False

        if message.get("v") != PROTOCOL_VERSION:
            self._fail(f"版本不匹配：agent={message.get('v')!r}")
            return False

        if message.get("session_id") != self.session_id:
            self._fail("session_id 不匹配（可能是非预期进程）")
            return False

        # 回一条 ready，告知 agent 已接受该会话（会自动签名）
        self.send(make_message(MSG_READY, ok=True))

        self.state = SessionState.READY
        self.handshake_done = True
        return True

    def probe(self, value: int = 0, action_id: str = "",
              timeout: float = DEFAULT_PROBE_TIMEOUT):
        """
        发送一次**无副作用**的 probe，验证 action_id 去重。

        返回 (ok, payload | reason)。

        action_id 缺省时按身份字段派生，使"同一逻辑动作"可被显式复用。
        """

        if self.state != SessionState.READY:
            return False, "会话未就绪"

        if not action_id:
            identity = {
                "plan_id": "a2p-probe-plan",
                "action_index": 0,
                "action_type": "probe",
                "params_hash": params_hash({"value": value}),
                "approval_ticket_id": "a2p-probe-ticket",
            }
            action_id = action_id_for(self._keys.action, identity)

        probe_id = uuid.uuid4().hex[:8]

        try:
            self.send(make_message(
                MSG_PROBE, probe_id=probe_id, action_id=action_id, value=value,
            ))
        except AgentLaunchError:
            return False, self.last_error

        message, reason = self._recv(timeout)

        if message is None:
            self._fail(f"等待 probe_ack 失败：{reason}")
            return False, self.last_error

        if message.get("type") != MSG_PROBE_ACK:
            self._fail(f"期望 probe_ack，收到 {message.get('type')!r}")
            return False, self.last_error

        if message.get("probe_id") != probe_id:
            self._fail("probe_ack 的 probe_id 不匹配（应答错位）")
            return False, self.last_error

        if message.get("action_id") != action_id:
            self._fail("probe_ack 的 action_id 不匹配")
            return False, self.last_error

        return True, {
            "value": message.get("value"),
            "replayed": message.get("replayed"),
            "action_id": action_id,
        }

    def ping(self, timeout: float = DEFAULT_PING_TIMEOUT) -> bool:
        """发一次心跳并等待 pong。"""

        if self.state != SessionState.READY:
            return False

        probe = uuid.uuid4().hex[:8]

        try:
            self.send(make_message(
                MSG_PING, session_id=self.session_id, probe_id=probe,
            ))
        except AgentLaunchError:
            return False

        message, reason = self._recv(timeout)

        if message is None:
            self._fail(f"等待 pong 失败：{reason}")
            return False

        if message.get("type") != MSG_PONG:
            self._fail(f"期望 pong，收到 {message.get('type')!r}")
            return False

        if message.get("probe_id") != probe:
            self._fail("pong 的 probe_id 不匹配（应答错位）")
            return False

        return True

    def stop(self, grace: float = DEFAULT_SHUTDOWN_GRACE) -> bool:
        """
        优雅停止。

        返回 True 表示 agent 在 grace 内确认并退出；
        False 表示超时后走了强制终止路径。
        """

        if self._proc is None:
            self.state = SessionState.STOPPED
            return True

        self.state = SessionState.STOPPING

        # ★ A2'-II 裁定 G-A：`grace` 是【整个优雅停止流程共享的总预算】，
        #   而不是每个阶段各得一份。这里用单调时钟建立【统一截止时间】，
        #   后续每个阶段只使用【剩余预算】。
        #
        #   G-A-4：预算耗尽 ≠ 进程已退出，也 ≠ 资源清理已完成。
        #   强制终止与资源回收可超出该预算（见 close()）。
        deadline = time.monotonic() + max(0.0, float(grace))

        def _remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        try:
            self.send(make_message(MSG_BYE))
        except AgentLaunchError:
            pass

        # 阶段 1：等 bye_ack —— 使用剩余预算
        message, reason = self._recv(_remaining())
        acked = (
            message is not None
            and message.get("type") == MSG_BYE_ACK
        )

        if not acked:
            self.last_error = f"未收到 bye_ack（{reason}）"

        # 阶段 2：等待进程真正退出 —— 使用【剩余】预算（不得重新获得完整 grace）
        if self._proc.wait(int(_remaining() * 1000)):
            self.state = SessionState.STOPPED
            return acked

        # 预算耗尽 -> 强制终止整个 Job。
        # 注意：此处【已超出共享预算】—— 属 G-A 允许的升级路径。
        self._proc.terminate(2)
        self.state = SessionState.STOPPED
        return False

    # -----------------------------------------------------------
    # 清理
    # -----------------------------------------------------------

    def _fail(self, reason: str) -> None:
        self.state = SessionState.FAULTED
        self.last_error = reason

    def _note_send_failure(self, reason: str, sealed: dict) -> None:
        """
        ★ ARONASEC-NIGHT-002 修复 1（P-1）：记录发送失败并进入故障态。

        契约：
            * 序号【保留】—— 不回滚 _out_seq（回滚会造成状态重用）
            * 发送结果标记为【未成功提交】
            * 会话进入 FAULTED，并置 _send_failed 禁止后续发送

        ⚠️ 只记录异常类型与描述，**不记录帧内容、密钥或明文**。
        ⚠️ 不推断对端是否收到 —— 部分帧可能已进入管道。
        """

        self._send_failed = True
        self._send_failure_reason = reason

        # 序号已分配的事实（仅记录，不回滚）
        seq_allocated = sealed.get("seq") if isinstance(sealed, dict) else None

        self._fail(
            f"发送未成功提交（序号 {seq_allocated} 已分配，不回滚）：{reason}"
        )

    def _note_cleanup_error(self, stage: str, exc: BaseException) -> None:
        """
        ★ A2'-II 裁定 T-1 / C2：记录**结构化**清理错误条目。

        字段与 C2 契约一致：`stage` · `error_type` · `message`。

        ⚠️ 只记录异常类型与描述，**不记录密钥、令牌或完整敏感消息**。
        """

        self.cleanup_errors.append({
            "stage": stage,
            "error_type": type(exc).__name__,
            "message": str(exc),
        })

    def _fail_if_established(self, reason: str) -> None:
        """
        对端在会话建立后消失 -> 标记 FAULTED。

        为什么需要这个区分：
            握手**之前**对端退出是正常的初始化失败路径，
            调用方会自己处理；但握手**之后**对端退出意味着
            会话已经不可信 —— 此时如果只是返回 "dead" 而不改状态，
            调用方可能看到 state==READY 并继续使用一个已死的会话
            （本机实测复现过：agent 因收到篡改消息退出，
             主进程却仍停在 READY）。
        """

        if self.handshake_done and self.state not in (
                SessionState.FAULTED, SessionState.STOPPED):
            self._fail(reason)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def close(self) -> None:
        """
        释放全部资源。

        即使从未成功 start()，也必须能安全调用。
        """

        # ---- 修复 2（L-3b）：三态 + 并发等待 ----
        #
        # 只有【首个】调用者能把 OPEN 原子地置为 CLOSING 并执行清理。
        # 其余调用者发现已 CLOSING/CLOSED 时，等待首轮完成后直接返回，
        # 既【不重复执行】清理，也【不重复记录】同一失败。
        with self._life_lock:

            if self.lifecycle in ("CLOSING", "CLOSED"):
                waiter = True
                proc = None
                channel = None
            else:
                waiter = False
                self.lifecycle = "CLOSING"
                self._close_owner = True

                # 置 _closing 之后，send()/业务 recv 都会立刻拒绝 ——
                # 因此不存在"检查后、使用前被关闭"的窗口。
                self._closing = True
                proc = self._proc
                self._proc = None
                channel = self.channel
                self.channel = None

        if waiter:
            # 有界等待首轮清理结束，避免永久阻塞
            self._close_done.wait(timeout=30.0)
            return

        try:
            self._close_once(proc, channel)
        finally:
            # 异常安全：无论清理成功或抛异常，都必须结束生命周期
            # 并唤醒等待者，否则并发 close 会永久阻塞。
            self.lifecycle = "CLOSED"
            self._close_done.set()

    def _close_once(self, proc, channel) -> None:
        """
        执行【唯一一轮】资源清理。

        仅由 close() 在持有首轮所有权时调用一次；
        并发/重复 close() 不会进入本方法（它们只等待）。
        """

        # ---- 锁外：进程 / 线程 / 句柄的清理（可能阻塞）----
        #
        # ★ A2'-II 裁定 T-1 / C2：cleanup_errors 保存【结构化条目】
        #   {"stage": str, "error_type": str, "message": str}
        #   T-1-c：把 SpawnedProcess 返回的错误与自身错误【合并】写入。
        #   T-1-d：每个可识别失败事件只记录一次，不在多层重复记录。
        if proc is not None:
            try:
                if proc.is_alive():
                    proc.terminate(2)
            except Exception as exc:                                  # noqa: BLE001
                self._note_cleanup_error("terminate", exc)

            # ★ 先 join 读取线程，再关句柄/fd。
            #
            # A1' 源码复核发现：直接 os.close(fd) 会与阻塞中的
            # os.read(fd) 竞争；CPython 未保证该场景下 fd 不被复用。
            # 终止进程会让管道读端收到 EOF，线程随即退出。
            readers_ok = self.join_readers(timeout=2.0)

            try:
                # T-1：close() 返回结构化错误条目列表（不再吞错）
                sub_errors = proc.close()
                for e in (sub_errors or []):
                    self.cleanup_errors.append({
                        "stage": e.get("stage", "spawned.close"),
                        "error_type": e.get("error_type", "Unknown"),
                        "message": e.get("message", ""),
                    })
            except Exception as exc:                                  # noqa: BLE001
                self._note_cleanup_error("proc.close", exc)

            if not readers_ok:
                # 让调用方能看到这次关闭是"不干净"的
                self.state = SessionState.FAULTED

        # ---- 认证通道 teardown（幂等）----
        #
        # ★ B-5：teardown 异常**不再被静默吞掉** ——
        #   记录到 self.cleanup_errors，调用方可检查。
        if channel is not None:
            try:
                channel.teardown("session close")

                # AuthChannel 自己也会收集清理异常，一并汇总上来
                for err in getattr(channel, "teardown_errors", []) or []:
                    self.cleanup_errors.append({
                        "stage": "channel.teardown",
                        "error_type": "TeardownStepError",
                        "message": str(err),
                    })

            except Exception as exc:                                  # noqa: BLE001
                self._note_cleanup_error("channel.teardown", exc)

        self.teardown_done = True

        # ★ A2'-II 裁定 PC-3：close() 完成后 AgentSession 不得再持有 KeyMaterial 引用。
        #   注意顺序：必须在 channel.teardown() 【之后】才解除，
        #   否则会先丢掉可用于调用 forget() 的引用（PC-3 明确禁止）。
        #
        #   🔴 解除引用【不等于】内存中的密钥字节已被擦除 —— 不得据此宣称安全擦除。
        self._keys = None

        if self.state not in (SessionState.STOPPED, SessionState.FAULTED):
            self.state = SessionState.STOPPED

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        try:
            if self.state == SessionState.READY:
                self.stop()
        finally:
            self.close()
        return False


def start_agent(project_root, **kwargs) -> AgentSession:
    """便捷函数：创建并启动会话。"""

    sess = AgentSession(Path(project_root), **kwargs)
    sess.start()
    return sess
