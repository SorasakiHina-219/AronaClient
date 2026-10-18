# AronaClient 桌面自主感知 — 剩余风险审查报告

**基线**: `5cfda5b` (branch `fix/awareness-thread-safety-and-reply`)  
**审查日期**: 2026-10-18  
**审查范围**: 只读，不修改任何文件

---

## 风险一：停止后的事件竞态

### 结论：**理论风险，实际不可复现；守卫已足够**

### 源码依据

| 位置 | 内容 |
|------|------|
| `core/desktop_awareness.py` L818-854 | `_on_event_from_worker()` 方法 |
| `ui.py` L8043-8085 | `AronaUI._on_awareness_alert()` 方法 |

### 守卫机制（两处，均已存在）

**守卫 1 — `is_stopped` 检查**（L828）：
```python
if self._state.is_stopped:
    logger.info("桌面感知：事件被丢弃（已停止）...")
    return
```

**守卫 2 — WorkerToken 隔离**（L836-847）：
```python
if (self._current_token is not None
    and event.worker_token is not None
    and event.worker_token.generation != self._current_token.generation):
    logger.info("桌面感知：事件被丢弃（旧 worker 延迟）...")
    return
```

### 为什么实际不可复现

1. **`stop()` 的实现**（`desktop_awareness.py`）：先设置 `_state = STOPPED`（原子操作），再调用 `_thread.wait(2000)` 等待 worker 线程退出。**所有守卫判断都在 worker 线程内执行，且 `_state` 在 worker 线程退出前就已经设为 STOPPED。**

2. **新启动的 awarenss** 使用全新的 `WorkerToken(generation=N+1)`，旧 token 的事件会在守卫 2 处被过滤。

3. **`_on_alert` 调用前两个守卫都已通过**，不存在"守卫通过后状态才变化"的时间窗口——因为 `_state` 和 `_current_token` 的写入都在同一个线程内、同一个方法中完成。

### 触发条件

理论上需要满足以下**全部**条件才会触发：
- `stop()` 完成后，旧 worker 的最后一个事件恰好在此时入队
- 该事件的 token 与新实例的 token **恰好相同**（generation 相同）
- 且 `is_stopped` 检查**恰好**在该事件到达时返回 False

实际中，每次 `start()` 都会递增 generation，旧事件的 token 必然不匹配。

### 影响

无实际影响。已验证：测试 E 中停止后 4 秒内无新事件到达。

### 建议

**无需修改。** 现有的两处守卫已经完备。如果在 `_on_awareness_alert` 内再加一个 `is_stopped` 检查，属于冗余防御，不改变行为。

---

## 风险二：后台回复线程的生命周期

### 结论：**低概率风险，窗口关闭时后台请求可能持续运行**

### 源码依据

| 位置 | 内容 |
|------|------|
| `ui.py` L3995 | `self.awareness_reply_threads = []` 初始化 |
| `ui.py` L8117 | `self.awareness_reply_threads.append((thread, worker))` |
| `ui.py` L8153-8159 | `_remove_awareness_reply_thread()` 清理逻辑 |
| `ui.py` L9619-9777 | `_stop_background_threads()` — **不包含 awareness_reply_threads** |
| `ui.py` L9809-9810 | `closeEvent` 中先调 `_stop_desktop_awareness()`，再调 `_stop_background_threads()` |

### 问题分析

1. **线程创建**（L8101-8125）：
   - `QThread(self)` — 以主窗口为 parent
   - 追加到 `self.awareness_reply_threads`
   - 连接 `thread.finished` → `_remove_awareness_reply_thread`（正常完成时清理）

2. **关闭窗口时**（L9809-9810）：
   - `_stop_desktop_awareness()` — 停止感知引擎，但**不等待**已有 AI 请求
   - `_stop_background_threads()` — 只处理 `worker_threads` 和 `speech_thread`，**完全跳过** `awareness_reply_threads`

3. **后果**：如果窗口关闭时恰有 AI 请求进行中：
   - 线程继续运行直到请求完成（可能数秒）
   - 线程持有 `self`（AronaUI）的引用，阻止窗口对象被 GC
   - 进程退出时 Qt 会强制终止线程（非优雅）

### 影响评估

| 场景 | 概率 | 影响 |
|------|------|------|
| 窗口关闭时无 AI 请求 | 高 | 无影响 |
| 窗口关闭时 AI 请求进行中 | 低（需恰好在此窗口期关闭） | 窗口不立即销毁，进程多运行数秒 |

### 最小修复方案

在 `_stop_background_threads()` 中增加对 `awareness_reply_threads` 的处理：

```python
# 在 _stop_background_threads() 的 targets 收集部分新增：
try:
    for index, pair in enumerate(
        list(getattr(self, "awareness_reply_threads", []))
    ):
        if isinstance(pair, (tuple, list)) and len(pair) >= 2:
            targets.append((f"awareness_reply#{index}", pair[0]))
except Exception:
    pass
```

**回归测试**：
1. 开启感知 → 等 AI 回复开始 → 立即关闭窗口 → 检查进程是否立即退出
2. 验证正常关闭时无额外日志

**剩余风险**：
- 如果 AI 请求耗时超过 1.5 秒（`wait_ms`），线程仍可能超时未被停止
- 此时线程会被 detach（`setParent(None)`），但 Python 引用仍保持，进程退出时由 OS 回收

---

## 风险三：普通聊天历史污染

### 结论：**已确认存在，感知回复会污染会话历史**

### 源码依据

| 位置 | 内容 |
|------|------|
| `core/ai_client.py` L144 | `self.messages = []` — 全局共享消息列表 |
| `core/ai_client.py` L2069 | `self.messages.append(request_message)` — 追加请求 |
| `core/ai_client.py` L2096 | `self.messages.append({"role": "assistant", ...})` — 追加回复 |
| `core/ai_client.py` L2103-2121 | `if self.current_session: self._save_current_session()` — 保存会话 |
| `ui.py` L696-712 | `AwarenessReplyWorker.run()` 调用 `self.ai_client.send_message(message)` |
| `core/ai_client.py` L1373-1412 | `_save_current_session()` 将 `self.messages` 写入 JSON |
| `ui.py` L6553-6560 | `display_conversation_messages()` 从 session 读取消息并显示 |

### 污染路径

```
用户聊天: send_message() → self.messages += [user_msg, assistant_msg] → 保存到 session_A
感知触发: send_message() → self.messages += [awareness_req, awareness_resp] → 保存到 session_A（同一 session!）
用户切换: switch_conversation(session_B) → self.messages = session_B.messages
          但旧的 awareness 消息已在 session_A 的 JSON 文件中
```

### 具体表现

1. **会话历史污染**：感知回复会被写入当前 session 的 JSON 文件。用户查看历史时会看到非对话性质的 AI 通知消息。

2. **上下文污染**：`_conversation_messages()`（L1309-1347）过滤掉非 user/assistant 角色，但感知消息的 role 是 "assistant"，内容以 "[桌面感知]" 开头。这些消息会出现在下一轮 AI 对话的 context 中。

3. **跨会话污染**：如果感知请求在用户切换会话**之前**发出但在**之后**完成，回复会写入新会话的 JSON（因为 `self.current_session` 已指向新会话）。

### 影响评估

| 场景 | 概率 | 影响 |
|------|------|------|
| 感知消息出现在会话历史中 | 高（每次感知都会） | 会话历史包含非对话内容 |
| 感知消息进入 AI context | 高 | 下一轮对话可能收到"噪音"上下文 |
| 跨会话污染 | 低（需恰好时序重叠） | 感知回复写入错误会话 |

### 最小修复方案

**方案 A：在 `send_message` 中跳过消息追加（推荐）**

在 `AIClient.send_message()` 中增加可选参数 `save_to_session=False`：

```python
def send_message(self, message, attachments=None, save_to_session=False):
    # ... 现有逻辑不变 ...
    if save_to_session:
        self.messages.append(request_message)
        self.messages.append({"role": "assistant", "content": answer})
        if self.current_session:
            self._save_current_session()
    return answer
```

`AwarenessReplyWorker.run()` 中调用：
```python
result = self.ai_client.send_message(message, save_to_session=True)
```

但这会改变 `send_message` 的签名，影响所有调用方。

**方案 B：在 `_on_awareness_reply_finished` 中回滚消息**

在 `_on_awareness_reply_finished` 中显示消息后，从 `self.messages` 中移除刚添加的感知消息：

```python
def _on_awareness_reply_finished(self, result: str) -> None:
    self._awareness_reply_busy = False
    logger.info("桌面感知：AI 回复完成")
    self.add_message("assistant", result)
    # 回滚 send_message 追加的感知消息
    self._rollback_awareness_messages()
    self._update_awareness_status_ui()

def _rollback_awareness_messages(self) -> None:
    """移除最近一轮感知请求/回复对。"""
    # 需要追踪感知消息的位置...
```

这需要追踪感知消息在 `self.messages` 中的位置，实现较复杂。

**方案 C：感知回复不走 `send_message`（最干净）**

在 `AwarenessReplyWorker` 中直接调用 API，不经过 `send_message`：

```python
def run(self):
    try:
        message = f"[桌面感知摘要]\n{self.awareness_description}\n\n请根据以上桌面观察结果..."
        # 直接调用 API，不修改 self.messages
        result = self._call_api_directly(message)
        self.finished.emit(result)
    except Exception as exc:
        self.error.emit(str(exc))
```

这需要在 `AIClient` 中新增一个不修改 `self.messages` 的 API 调用方法。

### 建议

**推荐方案 C**：最小化对现有代码的影响，感知回复完全独立于会话系统。

**回归测试**：
1. 开启感知 → 观察聊天中是否出现感知消息
2. 发送用户消息 → 检查会话历史是否只包含用户对话
3. 切换会话 → 检查感知消息是否出现在错误会话中

---

## 总结

| 风险 | 严重度 | 是否可复现 | 建议操作 |
|------|--------|-----------|---------|
| 风险一：停止后事件竞态 | 极低（理论） | 不可复现 | 无需修改 |
| 风险二：后台回复线程 | 低 | 可复现（低概率） | 在 `_stop_background_threads` 中增加处理 |
| 风险三：会话历史污染 | 中 | 可复现（每次感知） | 修改 `AwarenessReplyWorker` 或 `send_message` |

**本轮审查结束。等待用户授权后进行修复。**
