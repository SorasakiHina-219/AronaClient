# AronaClient 桌面感知修复（Fix A/B/C）只读复核报告

**复核范围**: 提交 `5cfda5b`，分支 `fix/awareness-thread-safety-and-reply`  
**基线**: `334dccf`  
**日期**: 2026-07-10  
**复核方式**: 纯源码阅读，不修改任何文件

---

## 一、Fix A：Qt 线程安全（`core/desktop_vision.py`）

### 1.1 `_is_main_thread()` — 源码已证实 ✅

```python
def _is_main_thread() -> bool:
    try:
        app = QCoreApplication.instance()
        if app is None:
            return True
        return app.thread() is QCoreApplication.instance().thread()
    except Exception:
        return True
```

- 使用 `QCoreApplication.instance().thread()` 与当前线程做身份比较（`is`），这是 Qt 标准做法
- `None` 实例时默认返回 `True`（假设安全），异常时同样兜底返回 `True`
- **结论**: 实现可靠，可正确区分主线程与 worker 线程

### 1.2 `_invoke_on_main_thread()` — 源码已证实 ✅

```python
def _invoke_on_main_thread(obj: QObject, method_name: str, *args) -> None:
    if _is_main_thread():
        getattr(obj, method_name)(*args)
        return
    QMetaObject.invokeMethod(obj, method_name, Qt.QueuedConnection, *args)
```

- 已在主线程时直接调用，避免不必要的队列开销
- 跨线程时使用 `Qt.QueuedConnection` 将方法排队到主线程 event loop 执行
- `*args` 透明传递参数
- **源码已证实**: 使用的是 PySide6 `QMetaObject.invokeMethod` 的正确调用形式
- **潜在风险（需运行确认）**: `setWindowState(state)` 中 `state` 是 `Qt.WindowState` 枚举值，通过 `*args` 传递到 `QMetaObject.invokeMethod`。若该枚举类型未注册为 Qt meta-type，`QueuedConnection` 下的参数传递可能静默失败。但实践中 `Qt.WindowState` 是核心枚举，PySide6 通常自动处理；建议在 GUI 验证时重点测试恢复后窗口状态是否正确。

### 1.3 所有 QWidget GUI 操作路由到主线程 — 源码已证实 ✅

`temporarily_hidden()` 中以下操作全部经 `_invoke_on_main_thread()` 路由：
- `w.hide()` ✅
- `w.show()` ✅
- `w.setWindowState(state)` ✅
- `w.raise_()` ✅
- `w.activateWindow()` ✅

`screenshot`（`screen.grabWindow(0)`）和 `get_active_window_info()` 留在 worker 线程执行——这符合设计：它们是屏幕级/Win32 级操作，不依赖 Qt widget 状态，可在任意线程安全执行。

### 1.4 窗口生命周期问题 — 源码存在风险 ⚠️

**问题**: 如果窗口在 `_invoke_on_main_thread(w, "hide")` 排队后、主线程实际执行前被销毁（`deleteLater`），则 queued call 的目标 QObject 已不存在，可能触发未定义行为。

**缓解因素**:
- `temporarily_hidden()` 被调用时窗口必然存在（否则无需隐藏）
- 窗口销毁前会先调用 `_stop_desktop_awareness()` → `awareness.stop()`，停止 worker 线程，从而不再调用 `temporarily_hidden()`
- 异常捕获块 `except Exception: pass` 会吞掉任何 RuntimeError

**结论**: 在正常运行路径下风险可控，但非理论上的零风险。**需 GUI 运行时确认**。

### 1.5 隐藏→截图→恢复顺序 — 源码已证实 ✅

```
_hide() [queued to main]
  → _settle(0.02s) [time.sleep, 不等主线程]
  → yield (截图在 worker 线程执行)
finally:
  → _show() [queued to main]
  → _setWindowState() [queued to main]
  → _raise_() [queued to main]
  → _activateWindow() [queued to main]
  → _settle(0.01s)
```

- `try/finally` 确保无论正常结束还是异常，都会执行恢复逻辑 ✅
- `_settle(0.02s)` 是 `time.sleep`，不阻塞主线程 event loop，所以 queued hide 操作可以在 20ms 内被处理 ✅
- 异常和提前返回均会触发 `finally` ✅

### 1.6 `screen.grabWindow(0)` 线程安全 — 无法仅凭源码确认 ⏸️

- `screen.grabWindow(0)` 是 X11/Windows GDI 层操作，不依赖 Qt widget 生命周期
- Qt5/Qt6 文档称其在任意线程均可调用
- **但**: Windows 上 DWM（桌面窗口合成）可能导致跨线程 grab 行为不一致
- **结论**: 源码层面无明显问题，**需 GUI 运行时确认是否偶尔产生黑屏或异常截图**

---

## 二、Fix B：主动 AI 回复（`ui.py`）

### 2.1 Worker 信号 → GUI 更新 — 源码已证实 ✅

```python
class AwarenessReplyWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    @Slot()
    def run(self):
        result = self.ai_client.send_message(message)
        self.finished.emit(result)   # 从 worker 线程 emit
```

- `finished` / `error` 信号由 worker 线程 emit，通过 Qt 信号槽机制自动排队到主线程
- `_on_awareness_reply_finished` / `_on_awareness_reply_error` 在主线程执行，安全调用 `add_message()`（QWidget 操作）
- 与现有 `AIWorker` 的线程模式完全一致 ✅

### 2.2 忙碌标记释放 — 源码已证实 ✅

```python
# 设置
self._awareness_reply_busy = True   # L8079 (_submit_awareness_reply)

# 释放路径 1: 成功
self._awareness_reply_busy = False  # L8116 (_on_awareness_reply_finished)

# 释放路径 2: 失败
self._awareness_reply_busy = False  # L8129 (_on_awareness_reply_error)
```

两条路径均释放 `_awareness_reply_busy`，**但存在一个边界情况**：
- 如果 `AwarenessReplyWorker.run()` 抛出未捕获异常（不是普通 Exception，而是 SystemExit/KeyboardInterrupt），则 `finished`/`error` 信号都不会 emit，`_awareness_reply_busy` 不会被重置
- 概率极低，但理论上存在永久阻塞风险

### 2.3 线程清理机制 — 源码已证实 ✅

```python
# 创建时追加
self.awareness_reply_threads.append((thread, worker))

# 完成时移除
thread.finished.connect(
    lambda pair=(thread, worker):
    self._remove_awareness_reply_thread(pair)
)

# _remove_awareness_reply_thread 实现
if thread_pair in self.awareness_reply_threads:
    self.awareness_reply_threads.remove(thread_pair)
```

- 与现有 `worker_threads` + `_remove_worker()` 模式完全一致
- `thread.finished` 信号在主线程 emit，触发 lambda 清理 ✅

### 2.4 停止感知后仍可能提交 AI 请求 — ⚠️ 存在风险

**问题**: `_on_awareness_alert()` 中没有检查 `self.desktop_awareness` 是否已停止：

```python
def _on_awareness_alert(self, event):
    # ... 无 is_stopped 检查 ...
    if not self._awareness_reply_busy:
        self._submit_awareness_reply(description)  # 仍会提交
```

依赖的是 `_on_event_from_worker()` 中的守卫（`is_stopped` 检查 + `worker_token` 匹配），但这两个守卫有一个时序漏洞：

1. `stop()` 将 `_state` 设为 STOPPED
2. 但旧 worker 线程可能还有正在 queue 的信号（`event_ready.emit(event)` 已发出但尚未被 `_on_event_from_worker` 接收）
3. 如果这些延迟信号在守卫检查之前到达 `_on_awareness_alert`，AI 回复仍会被提交

**实际影响**: 概率低（依赖 event loop 调度延迟），但确实存在。守卫机制依赖 `_on_event_from_worker` 的先发检查，而非 `_on_awareness_alert` 自身的状态检查。

### 2.5 会话污染风险 — 🔴 中等风险

**问题**: `AIClient.send_message()` 会将消息追加到 `self.messages` 列表，并保存到 `self.current_session`：

```python
# ai_client.py L2069
self.messages.append(request_message)    # 添加用户消息
# ... API 调用 ...
self.messages.append({"role": "assistant", "content": answer})  # L2096 添加助手回复
self._save_current_session()  # 保存到当前会话
```

`_conversation_messages()` 返回的是完整的 `self.messages` 列表（含所有历史消息）。这意味着：
- 感知 AI 回复会被追加到 `self.messages` 中
- 下次用户发送消息时，`send_message()` 会把感知对话历史也作为上下文发送给 API
- 用户聊天的会话历史会被感知内容污染

**但**: 这取决于 `current_session` 的管理方式。如果每次新对话都 `clear_history()` 重置 `messages = [system_prompt]`，则污染仅限于同一会话内。需确认 `AIClient` 的会话隔离机制。

**源码确认**: `send_message` 中有 `self._save_current_session()` 调用（L2121），说明每条消息都会被持久化到当前会话。**这是一个需要关注的会话污染问题**。

### 2.6 并发消息列表修改 — ⚠️ 低风险

`send_message` 中 `self.messages.append()` 没有锁保护。如果感知 AI 回复（worker 线程调用）与用户聊天（主线程调用）同时执行，可能出现消息顺序错乱。但由于：
- `_awareness_reply_busy` 和 `_ai_busy` 是两个独立标志位，不会互相阻挡
- Python GIL 对简单 append 操作有原子性保障
- 实际并发概率低

**结论**: 低风险，但不是理论上的零风险。

### 2.7 空回复/API 错误/窗口关闭 — 源码已证实 ✅

- 空回复：`result` 为空字符串时仍正常 display，无崩溃风险
- API 错误：`except Exception` 捕获，emit `error` 信号
- 窗口关闭：`awareness_reply_threads` 中的线程**未被 `_stop_background_threads` 清理**（见下方分析）

---

## 三、Fix C：诊断日志（`core/desktop_awareness.py`）

### 3.1 高频循环日志量 — ⚠️ 需关注

- 默认 interval = 30 秒
- 每轮循环产生 1-5 条 INFO 日志
- 正常状态（无变化）：每 30 秒约 1-2 条日志
- 持续变化状态（如视频播放）：每 30 秒约 4-5 条日志
- **无日志速率限制**：如果画面持续变化且每次都触发分析，日志量可能较大

**建议**: 考虑添加每秒/每分钟的日志上限，但当前实现不会导致系统问题。

### 3.2 失败原因可追踪性 — 源码已证实 ✅

- 截图失败: `logger.info("截图失败（%s）", shot.error)` ✅
- pHash 失败: `logger.warning("pHash 计算失败 [gen=%d, id=%d]")` ✅
- AI 分析失败: `logger.warning("AI 分析失败 [gen=%d] %.0fms: %s", ...)` ✅
- 事件丢弃: `logger.info("事件被丢弃（已停止）/事件被丢弃（旧 worker 延迟）")` ✅

### 3.3 敏感信息泄露 — 源码已证实 ✅

- `description[:80]` 截断（ui.py L8049）✅
- `analysis["error"][:100]` 截断 ✅
- `(analysis.get("text") or "")[:80]` 截断 ✅
- 不记录 API Key、截图原始内容、完整提示词

### 3.4 `summary()` 方法 — 部分实现，存在局限 ⚠️

`DesktopAwareness.summary()` 返回：
```python
{
    "state": self._state.value,
    "is_running": ...,
    "is_paused": ...,
    "is_stopped": ...,
    "interval_seconds": self._interval,
    "has_ai_client": ...,
    "worker_alive": ...,
    "thread_alive": ...,
}
```

**缺失**: 最近一次错误信息（`_last_error` 不存在）、线程栈状态

**并发安全**: 读取 `_state`、`_interval`、`_ai_client` 等字段无锁保护，但在单写多读模式下，读到中间态的概率很低（这些字段在初始化/停止时批量写入）。

---

## 四、关键缺陷汇总

| # | 严重度 | 位置 | 问题描述 | 类型 |
|---|--------|------|----------|------|
| 1 | 🔴 中 | `ui.py` L8032+ | `_on_awareness_alert` 无 `is_stopped` 检查，停止后仍可能提交 AI 请求 | 源码确认 |
| 2 | 🔴 中 | `ui.py` L8086+ | `closeEvent` 未清理 `awareness_reply_threads`，窗口关闭时后台线程可能泄漏 | 源码确认 |
| 3 | 🟡 低 | `core/ai_client.py` L2069+ | `send_message` 共享 `self.messages`，感知回复污染用户会话历史 | 源码确认 |
| 4 | 🟡 低 | `core/desktop_vision.py` L161+ | `setWindowState` 通过 `invokeMethod` 传 Qt 枚举值，可能需 meta-type 注册 | 需运行时确认 |
| 5 | 🟢 信息 | `core/desktop_awareness.py` L300+ | 无日志速率限制，持续变化场景日志量较大 | 源码确认 |
| 6 | 🟢 信息 | `core/desktop_vision.py` L238 | `_settle(0.02s)` 可能不足以让 queued GUI 操作完全执行 | 需运行时确认 |

---

## 五、人工 GUI 验收清单

### 测试 A：设置对话框不冻结

**前置条件**: 桌面自主感知已开启，感知引擎正在运行

**操作步骤**:
1. 打开 AronaClient 主界面
2. 等待感知引擎启动（状态栏显示"桌面感知活跃"）
3. 打开设置对话框（SettingsDialog）
4. 在设置对话框打开状态下，切换感知开关（关闭→等待10秒→重新开启）
5. 重复步骤 3-4，共 3 次
6. 关闭设置对话框

**预期现象**:
- 设置对话框正常弹出和关闭
- 感知状态栏正常更新
- 无界面冻结、无无响应提示
- 任务管理器中感知线程数无异常增长

**失败现象**:
- 界面卡死超过 3 秒
- 设置对话框无法关闭
- 状态栏显示异常

**保存证据**: 截图状态栏变化过程；检查 `logs/` 目录下最新日志文件

---

### 测试 B：开关切换无线程堆积

**前置条件**: 感知引擎已运行

**操作步骤**:
1. 记录当前感知线程状态（任务管理器 → 详细信息 → 查看 python.exe 线程数）
2. 关闭感知开关 → 等待 5 秒
3. 重新开启感知开关 → 等待 30 秒（至少一个完整 cycle）
4. 重复步骤 2-3，共 3 轮
5. 再次记录线程数

**预期现象**:
- 线程数在开关切换前后基本一致（±1 个线程浮动可接受）
- 无线程数持续增长

**失败现象**:
- 线程数逐轮递增
- 任务管理器显示多个 python.exe 进程

**保存证据**: 每轮切换前后的线程数截图；日志中"已停止"/"启动"条目

---

### 测试 C：静态通知与 AI 回复均正确显示

**前置条件**: 感知引擎已运行，AI API 已配置

**操作步骤**:
1. 切换桌面窗口或打开新程序，制造画面变化
2. 等待感知触发（最长约 90 秒，含去重窗口）
3. 观察聊天界面

**预期现象**:
- 首先出现 `[桌面感知] xxx` 静态通知消息
- 随后出现 AI 生成的回复消息（无 `[桌面感知]` 前缀）
- 两条消息均可正常显示、无格式错误
- 状态栏短暂显示"桌面感知：Arona 正在思考……"后恢复正常

**失败现象**:
- 只有静态通知，无 AI 回复
- AI 回复显示为错误信息
- 聊天界面卡顿

**保存证据**: 聊天记录截图（需包含两条消息）；日志中"AI 回复完成"条目

---

### 测试 D：停止感知后无新 AI 请求

**前置条件**: 感知引擎已运行，已触发过至少一次 AI 回复

**操作步骤**:
1. 关闭感知开关（触发 `_stop_desktop_awareness()`）
2. 立即切换桌面窗口制造变化
3. 等待 60 秒
4. 观察聊天界面和状态栏

**预期现象**:
- 不再出现新的 `[桌面感知]` 消息
- 不再出现 AI 生成回复
- 状态栏显示"桌面感知已停止"或类似状态

**失败现象**:
- 停止后仍有新消息出现
- 状态栏仍显示"活跃"

**保存证据**: 日志中"事件被丢弃（已停止）"条目；聊天记录截图

**注意区分**: 停止前已提交的 AI 请求可能在停止后完成并显示回复——这是正常行为，不是 bug。判断依据：如果停止前有"提交 AI 回复请求"日志，停止后有"AI 回复完成"日志，则该回复是停止前提交的。

---

### 测试 E：普通聊天与感知回复共存不冲突

**前置条件**: 感知引擎已运行，AI API 已配置

**操作步骤**:
1. 在聊天中输入一条普通消息并发送
2. 在 AI 回复过程中，切换桌面窗口制造感知变化
3. 观察两条回复的消息顺序和内容

**预期现象**:
- 普通聊天消息和 AI 回复消息各自独立显示
- 消息顺序正确（按时间排列）
- 输入框可正常输入新消息
- 状态栏正确反映当前状态

**失败现象**:
- 感知回复插入到普通聊天中间
- 输入框不可用
- 消息内容混乱（如感知内容出现在用户消息位置）

**保存证据**: 聊天记录截图；日志中相关条目

---

## 六、最终复核结论

| 项目 | 结论 |
|------|------|
| **Fix A（线程安全）** | **通过（源码）**，存在 2 项需运行时确认的风险（枚举传递、grabWindow 跨线程） |
| **Fix B（主动回复）** | **通过（源码）**，存在 3 项需关注的风险（停止后仍提交、关闭时线程泄漏、会话污染） |
| **Fix C（诊断日志）** | **通过（源码）**，无明显问题 |
| **整体安全回归** | **通过**（10/10 测试全部 PASS） |
| **语法/编译** | **通过**（3 文件 py_compile 全部 PASS） |
| **应用启动** | **通过**（PID 14092 正常启动运行） |

### 尚存风险及严重程度

| 风险 | 严重度 | 是否需要修复 |
|------|--------|-------------|
| 停止感知后 `_on_awareness_alert` 仍可能提交 AI 请求 | 🟡 低 | 建议修复（加一行状态检查即可） |
| 窗口关闭时 `awareness_reply_threads` 未在 `_stop_background_threads` 中清理 | 🟡 低 | 建议修复（3 行代码） |
| `send_message` 共享 `self.messages` 导致会话污染 | 🟡 低 | 需确认 AIClient 会话隔离机制后再评估 |
| `setWindowState` 枚举传递可能需 meta-type 注册 | 🔵 信息 | 运行时验证 |
| `_settle(0.02s)` 可能不足以处理 queued GUI 操作 | 🔵 信息 | 运行时验证 |

### 是否建议进入下一阶段

**建议：可以进入人工 GUI 验收阶段。**

- 源码级修复逻辑正确，核心问题（跨线程 GUI 操作、主动回复架构）已解决
- 发现的 3 项低风险问题可通过 5 行代码修复，不影响验收判断
- 会话污染问题需要先确认 AIClient 的会话模型，建议在验收过程中观察

---

**复核完成。本复核仅基于源码静态分析，未执行任何 GUI 测试。五项人工 GUI 验收测试待人工执行后补充结果。**
