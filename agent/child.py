# agent/child.py
# AronaClient A2'-I —— agent 进程入口（已接入认证）
#
# ============================================================
# 本进程的硬性能力边界
# ============================================================
#
# 只做：
#   * 接收一次密钥帧（自举），建立认证通道
#   * 认证地收发 hello / ping / pong / probe / bye
#   * 版本协商 · 心跳 · 优雅停止
#
# 【不】做：
#   * 不 import pyautogui（启动时主动断言）
#   * 不加载 ComputerController / RemoteController / desktop_vision
#   * 不读取桌面、不截图、不读窗口信息
#   * 动作白名单为空
#
# ============================================================
# 初始化状态机（严格三阶段）
# ============================================================
#
#   PHASE_WAIT_KEY ──收到合法 key 帧──► PHASE_HANDSHAKE ──hello 已发──► PHASE_READY
#        │                                   │                            │
#        └──── 任何非法/重复 key 帧 ──────────┴──── 任何认证失败 ──────────┘
#                          │
#                          ▼
#                    fail-closed：退出
#
# 关键约束：
#   * key 帧**只接受一次**；第二次（无论什么阶段）立即退出
#   * 在 PHASE_WAIT_KEY 阶段收到**任何**其它消息 → 立即退出
#     （旧版未认证协议的消息不会被静默接受）
#   * 进入 PHASE_HANDSHAKE 之后，所有入站消息都必须通过 AuthChannel.open()
#
# ============================================================
# 🔴 认证能证明什么
# ============================================================
#
# 验签通过 == "消息由持有 K_frame 的一方生成"。
# 它**不等于**用户批准，**不等于**主进程未被攻陷。
# 密钥就在主进程内存里，被攻陷即可随意签名。

from __future__ import annotations

import os
import queue
import sys
import threading
import time

# ---- 在任何其他 import 之前抓走真正的 stdout ----
_REAL_STDOUT = sys.stdout.buffer

# 之后所有 print() 都走 stderr，不会污染协议
sys.stdout = sys.stderr

from agent.authchannel import AuthChannel, AuthError  # noqa: E402
from agent.crypto import KeyMaterial  # noqa: E402
from agent.protocol import (  # noqa: E402
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

# 本进程绝不允许出现的能力
FORBIDDEN_MODULES = ("pyautogui",)

DEFAULT_IDLE_TIMEOUT = 10.0

# 初始化阶段
PHASE_WAIT_KEY = "WAIT_KEY"
PHASE_HANDSHAKE = "HANDSHAKE"
PHASE_READY = "READY"

# ---- B-6：各阶段允许的消息类型（真实门禁）----
#
# A2'-I 审计发现：PHASE_READY 只是被置位，**从未在处理路径中被检查**，
# 因此握手完成前的 probe / bye 会被正常处理。这里把它变成实际约束。
#
#   WAIT_KEY  —— 由独立的初始化循环处理，只接受 key 帧（见 run()）
#   HANDSHAKE —— 只允许 ready（完成握手）与 ping（保活探测）
#   READY     —— 允许全部已认证业务类型
PHASE_ALLOWED = {
    PHASE_HANDSHAKE: frozenset({MSG_READY, MSG_PING}),
    # ★ A2'-II 裁定 PC-1：READY 是【一次性】状态转换。
    #   进入 READY 后再次收到 MSG_READY，必须按协议错误拒绝并终止会话，
    #   不得重复触发握手副作用。因此这里【不再包含 MSG_READY】。
    PHASE_READY: frozenset({MSG_PING, MSG_PROBE, MSG_BYE}),
}

# 退出码（便于测试断言具体失败原因）
EXIT_OK = 0
EXIT_TIMEOUT = 4
EXIT_PROTOCOL = 5
EXIT_VERSION = 6
EXIT_INPUT_CAPABILITY = 7
EXIT_INIT = 8
EXIT_AUTH = 10


def assert_no_input_capability(where: str = "") -> None:
    """纵深防御：本进程不得加载任何输入控制能力。"""

    loaded = [m for m in FORBIDDEN_MODULES if m in sys.modules]

    if loaded:
        raise RuntimeError(
            f"agent 进程不应加载输入控制模块，但发现：{loaded}"
            + (f"（{where}）" if where else "")
        )


def _send(message: dict) -> None:
    _REAL_STDOUT.write(encode_frame(message))
    _REAL_STDOUT.flush()


_STDIN_QUEUE = None
_STDIN_LOCK = threading.Lock()


def _ensure_stdin_reader():
    """后台读取线程 + 队列（空闲超时才能真正生效，见 A1' 复核）。"""

    global _STDIN_QUEUE

    with _STDIN_LOCK:

        if _STDIN_QUEUE is not None:
            return _STDIN_QUEUE

        q = queue.Queue()

        def _loop():
            while True:
                try:
                    chunk = os.read(0, 8192)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                q.put(chunk)
            q.put(None)

        threading.Thread(target=_loop, name="agent-stdin", daemon=True).start()
        _STDIN_QUEUE = q
        return q


def _read_frame(buffer: bytearray, deadline: float):
    """读一帧（带真正的空闲超时）。返回 (message|None, reason)。"""

    q = _ensure_stdin_reader()

    while True:

        try:
            message, consumed = decode_frame_from_buffer(buffer)
        except FrameError as exc:
            return None, f"error:{exc}"

        if message is not None:
            del buffer[:consumed]
            return message, "ok"

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            return None, "timeout"

        try:
            chunk = q.get(timeout=min(remaining, 0.2))
        except queue.Empty:
            continue

        if chunk is None:
            return None, "eof"

        buffer.extend(chunk)


def run() -> int:
    """agent 主循环。返回进程退出码。"""

    try:
        assert_no_input_capability("启动时")
    except RuntimeError as exc:
        print(f"[agent] 拒绝启动：{exc}", file=sys.stderr)
        return EXIT_INPUT_CAPABILITY

    idle_timeout = float(os.environ.get("ARONA_AGENT_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT))
    session_hint = os.environ.get("ARONA_AGENT_SESSION", "")

    buffer = bytearray()
    started = time.monotonic()
    deadline = started + idle_timeout

    channel = None
    session_id = session_hint
    phase = PHASE_WAIT_KEY

    # ============================================================
    # 阶段 1：等待密钥帧（唯一不签名的消息）
    # ============================================================
    while phase == PHASE_WAIT_KEY:

        message, reason = _read_frame(buffer, deadline)

        if message is None:
            if reason == "timeout":
                print(f"[agent] 等待密钥帧超时（{idle_timeout}s），退出。", file=sys.stderr)
                return EXIT_TIMEOUT
            if reason == "eof":
                print("[agent] 等待密钥帧时 stdin 关闭，退出。", file=sys.stderr)
                return EXIT_OK
            print(f"[agent] 等待密钥帧时协议错误：{reason}", file=sys.stderr)
            return EXIT_PROTOCOL

        deadline = time.monotonic() + idle_timeout

        # ★ 只接受 key 帧。任何其它消息都说明对端在用未认证协议 —— 立即拒绝。
        if message.get("type") != "key":
            print(
                f"[agent] 初始化阶段只接受密钥帧，收到 {message.get('type')!r} —— "
                "拒绝（不接受未认证的业务消息）",
                file=sys.stderr,
            )
            return EXIT_INIT

        # 校验密钥帧内容
        try:
            keys = KeyMaterial.from_frame_fields(message)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[agent] 密钥帧格式非法：{exc}", file=sys.stderr)
            return EXIT_INIT

        frame_sid = message.get("session_id")

        if not isinstance(frame_sid, str) or not frame_sid:
            print("[agent] 密钥帧缺少 session_id，退出。", file=sys.stderr)
            return EXIT_INIT

        if session_hint and frame_sid != session_hint:
            print(
                f"[agent] 密钥帧 session_id 与启动参数不符："
                f"{frame_sid!r} != {session_hint!r}",
                file=sys.stderr,
            )
            return EXIT_INIT

        session_id = frame_sid
        channel = AuthChannel(keys, session_id, role="agent")
        phase = PHASE_HANDSHAKE

    # ============================================================
    # 阶段 2：发出已签名的 hello
    # ============================================================

    def _finish(code: int) -> int:
        """
        ★ B-4：所有退出路径都必须经过这里。

        A2'-I 审计发现：EXIT_PROTOCOL / EXIT_TIMEOUT / EXIT_OK 等路径
        直接 return，**跳过了 channel.teardown()**，进程内认证状态未被清理。

        清理失败不会被静默吞掉 —— 记录到 stderr 供诊断，
        同时继续返回原退出码（清理失败不应掩盖真正的退出原因）。
        """

        if channel is not None:
            try:
                channel.teardown(f"exit {code}")

                for err in getattr(channel, "teardown_errors", []) or []:
                    print(f"[agent] 清理告警：{err}", file=sys.stderr)

            except Exception as exc:                                  # noqa: BLE001
                print(
                    f"[agent] 清理失败（可观测）：{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

        return code

    try:
        _send(channel.seal(make_message(
            MSG_HELLO,
            pid=os.getpid(),
            protocol=PROTOCOL_VERSION,
        )))
    except AuthError as exc:
        print(f"[agent] 发送 hello 失败：{exc}", file=sys.stderr)
        return _finish(EXIT_AUTH)

    # ============================================================
    # 阶段 3：认证主循环
    # ============================================================
    while True:

        message, reason = _read_frame(buffer, deadline)

        if message is None:
            if reason == "timeout":
                print(f"[agent] 空闲超时（{idle_timeout}s），自行退出。", file=sys.stderr)
                return _finish(EXIT_TIMEOUT)
            if reason == "eof":
                print("[agent] stdin 关闭，退出。", file=sys.stderr)
                return _finish(EXIT_OK)
            print(f"[agent] 协议错误：{reason}", file=sys.stderr)
            return _finish(EXIT_PROTOCOL)

        deadline = time.monotonic() + idle_timeout

        # ---- 统一认证：所有入站消息都必须先过这一关 ----
        ok, why = channel.open(message)

        if not ok:
            print(f"[agent] 认证失败：{why} —— 会话不可继续，退出。", file=sys.stderr)
            try:
                channel.teardown("auth failure")
            except Exception:
                pass
            return EXIT_AUTH

        mtype = message.get("type")

        # ★ B-6：阶段门禁 —— 握手完成前不得处理业务消息。
        #
        # 消息已经通过 channel.open() 认证；这里再检查它在**当前阶段**
        # 是否被允许。两者缺一不可：
        #   认证 回答"是谁发的、有没有被篡改"
        #   门禁 回答"这个阶段该不该接受这类消息"
        allowed = PHASE_ALLOWED.get(phase, frozenset())

        if mtype not in allowed:
            print(
                f"[agent] 阶段 {phase} 不接受消息类型 {mtype!r} —— "
                "拒绝（业务消息在握手完成前不得处理）",
                file=sys.stderr,
            )
            return _finish(EXIT_PROTOCOL)

        # ---- 业务处理（此处消息已认证且阶段允许） ----
        try:

            if mtype == MSG_PING:

                _send(channel.seal(make_message(
                    MSG_PONG,
                    probe_id=message.get("probe_id"),
                    uptime_ms=int((time.monotonic() - started) * 1000),
                )))
                continue

            if mtype == MSG_READY:
                # master 接受了会话
                phase = PHASE_READY
                continue

            if mtype == MSG_PROBE:
                # 无副作用探测：验证 action_id 去重
                action_id = message.get("action_id")

                if not isinstance(action_id, str) or not action_id:
                    print("[agent] probe 缺少 action_id，退出。", file=sys.stderr)
                    return _finish(EXIT_PROTOCOL)

                cached = channel.cached_action(action_id)

                if cached is not None:
                    # 重复请求：返回缓存，不重新处理
                    _send(channel.seal(make_message(MSG_PROBE_ACK, **{
                        "probe_id": message.get("probe_id"),
                        "action_id": action_id,
                        "replayed": True,
                        "value": cached.get("value"),
                    })))
                    continue

                # 首次处理（无副作用）
                reply = {"value": (message.get("value") or 0) + 1}

                try:
                    channel.remember_action(action_id, reply)
                except AuthError as exc:
                    # 缓存满 -> 传播失败，不能忽略后继续通信
                    print(f"[agent] 动作缓存失败：{exc} —— 退出。", file=sys.stderr)
                    try:
                        channel.teardown("action log full")
                    except Exception:
                        pass
                    return EXIT_AUTH

                _send(channel.seal(make_message(MSG_PROBE_ACK, **{
                    "probe_id": message.get("probe_id"),
                    "action_id": action_id,
                    "replayed": False,
                    "value": reply["value"],
                })))
                continue

            if mtype == MSG_BYE:

                _send(channel.seal(make_message(
                    MSG_BYE_ACK,
                    uptime_ms=int((time.monotonic() - started) * 1000),
                )))
                break

            # 其它已认证但本阶段不处理的类型
            print(f"[agent] 忽略已认证消息类型：{mtype}", file=sys.stderr)

        except AuthError as exc:
            print(f"[agent] 处理 {mtype} 时认证错误：{exc} —— 退出。", file=sys.stderr)
            return _finish(EXIT_AUTH)

    # ============================================================
    # 收尾
    # ============================================================
    try:
        assert_no_input_capability("退出时")
    except RuntimeError as exc:
        print(f"[agent] 退出前检查失败：{exc}", file=sys.stderr)
        return _finish(EXIT_INPUT_CAPABILITY)

    # 正常收尾也统一走 _finish（幂等，重复调用安全）
    print(
        f"[agent] 正常退出（uptime={time.monotonic() - started:.2f}s）",
        file=sys.stderr,
    )
    return _finish(EXIT_OK)


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:                                       # noqa: BLE001
        print(f"[agent] 未捕获异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 9


if __name__ == "__main__":
    sys.exit(main())
