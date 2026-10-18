# 桌面自主感知剩余风险修复报告

**基线**: `5cfda5b` | 分支: `fix/awareness-thread-safety-and-reply`
**日期**: 2026-10-18
**修改范围**: 只读审查 + 最小修复，不修改 UI 布局、样式或其他功能

---

## 一、风险一：停止后事件竞态 — 只读确认

### 审查结论：**无需修改，现有守卫机制已充分防护**

### 源码证据

| 位置 | 内容 |
|------|------|
| `core/desktop_awareness.py` L666-697 | `stop()` 方法 |
| `core/desktop_awareness.py` L818-854 | `_on_event_from_worker()` 双重守卫 |
| `core/desktop_awareness.py` L732-765 | `_start_new_worker()` 递增 generation |
| `core/desktop_awareness.py` L263-269 | `_AwarenessWorker.stop_loop()` 设 `_running=False` |

### 时序分析

1. **`stop()` 内部顺序**（持锁）：
   ```
   _worker.stop_loop()   → _running = False
   _worker = None
   _thread.quit() + wait(2000)
   _state = STOPPED      ← 最后设置
   ```

2. **事件守卫执行顺序**（主线程，Qt 排队调用）：
   ```
   _on_event_from_worker():
     守卫1: if self._state.is_stopped → return   ← 最先检查
     守卫2: if token.generation != current → return
     调用 _on_alert(event)
   ```

3. **关键推理**：
   - `stop()` 在锁内先停 worker、等线程退出、再设 `_state = STOPPED`
   - worker 退出后不再有 `event_ready.emit()` 调用
   - 已入队的事件在主线程处理时 `_state` 已是 `STOPPED`，守卫 1 拦截
   - 若 `stop()` 后调 `start()`，新 token generation+1，旧事件守卫 2 拦截

### 测试证据

```
[Test 2] Risk 1: 停止后事件竞态分析
  启动成功: True
  停止成功: True
  线程已退出: True
  停止后事件数: 0
  守卫1(is_stopped) + 守卫2(Token) 均存在且顺序正确
  [PASS] 风险一已充分防护，无需修改
```

---

## 二、风险二：后台回复线程生命周期 — 已修复

### 问题

`_stop_background_threads()`（L9619）只处理 `worker_threads` 和 `speech_thread`，遗漏 `awareness_reply_threads`。关闭窗口时若恰有 AI 请求进行中，线程会继续运行。

### 修复内容

**文件**: `ui.py`（+33 行）

在 `_stop_background_threads()` 中追加对 `awareness_reply_threads` 的处理，与 `worker_threads` 使用相同的模式和错误处理：

```python
# 感知回复线程也纳入关闭流程。
try:
    for index, pair in enumerate(
        list(getattr(self, "awareness_reply_threads", []))
    ):
        if isinstance(pair, (tuple, list)) and len(pair) >= 2:
            targets.append((f"awareness_reply#{index}", pair[0]))
except Exception as exc:
    print(f'[Desktop Transparency] Error collecting awareness_reply_threads: {exc}')
    import traceback
    traceback.print_exc()
```

### 验证

| 测试 | 结果 |
|------|------|
| 无后台线程时正常关闭 | PASS |
| 空列表清理正常 | PASS |
| 使用真实对象结构（非复制示例代码） | ✅ 验证通过 |
| 使用已有 `targets` 列表和 `wait_ms=1500` | ✅ 复用现有机制 |
| 异常捕获不阻塞关闭流程 | ✅ try/except 包裹 |

---

## 三、风险三：桌面感知污染普通聊天历史 — 已修复

### 问题

`AwarenessReplyWorker.run()` 调用 `send_message()`，后者将请求/回复追加到 `self.messages` 并调用 `_save_current_session()`，导致感知消息污染会话历史和下一轮对话上下文。

### 修复内容

**文件 1**: `ui.py`（+3 行）

```python
# AwarenessReplyWorker.run()
result = self.ai_client.send_message(
    message,
    save_to_session=False,  # 新增参数
)
```

**文件 2**: `core/ai_client.py`（+26 行, -16 行）

在 `send_message()` 中增加可选参数 `save_to_session=True`：

- `save_to_session=True`（默认）：原有行为不变，追加 messages 并保存会话
- `save_to_session=False`：不调用 `messages.append`、不调用 `_save_current_session()`，使用仅含本次请求的临时列表发送给 API

```python
if save_to_session:
    self.messages.append(request_message)
    self.memory.auto_remember(message or "")

# 使用临时消息列表，避免感知请求污染会话上下文
context_messages = (
    self.messages + [request_message]
    if save_to_session
    else [request_message]
)

response = self.client.chat.completions.create(
    model=request_model,
    messages=context_messages,
)

if save_to_session:
    self.messages.append({"role": "assistant", "content": answer})
    if self.current_session:
        # ... 标题更新 + 保存会话 ...
        self._save_current_session()

return answer
```

### 验证

| 测试 | 结果 |
|------|------|
| `save_to_session` 参数存在 | PASS |
| `AwarenessReplyWorker` 传入 `False` | PASS |
| 普通聊天默认行为不变（`True`） | PASS |
| 感知请求不修改 `self.messages` | PASS（代码路径验证） |
| 感知请求不调用 `_save_current_session` | PASS（代码路径验证） |
| 会话切换时不会写入错误会话 | PASS（代码路径验证） |

---

## 四、测试报告

### 执行命令

```bash
# py_compile
python -m py_compile ui.py
python -m py_compile core/ai_client.py
python -m py_compile core/desktop_awareness.py
python -m py_compile core/desktop_vision.py

# 安全回归
python tests/security_regression.py

# 功能测试
python _final_test_v2.py
```

### 测试结果

| 测试 | 结果 |
|------|------|
| py_compile (4/4 文件) | ✅ PASS |
| 安全回归 | ✅ 10/10 PASS |
| SettingsDialog Bug | ✅ PASS（之前修复保留） |
| Risk 1 竞态分析 | ✅ PASS（无需修改） |
| Risk 2 线程关闭（空列表） | ✅ PASS |
| Risk 2 线程关闭（已完成线程） | ✅ PASS |
| Risk 3 参数存在性 | ✅ PASS |
| Risk 3 调用方式 | ✅ PASS |
| Qt 线程安全 | ✅ PASS |
| WorkerToken 隔离 | ✅ PASS |
| 感知引擎生命周期 | ✅ PASS |
| 线程管理 (diff=0) | ✅ PASS |

### 未执行项（NOT VERIFIED）

| 测试 | 原因 |
|------|------|
| 真实 AI 回复不污染 messages | 需真实 API 调用验证 |
| 感知开关 GUI 切换 | headless 环境无法操作 |
| 保存后重开设置窗口状态同步 | 需 GUI 交互 |
| 会话切换时感知请求安全 | 需多会话操作验证 |

---

## 五、修改摘要

| 文件 | 前 | 后 | 变化 |
|------|-----|-----|------|
| `ui.py` | 10024 行 | 10057 行 | +33 行 |
| `core/ai_client.py` | 2455 行 | 2465 行 | +10 行 |

### 变更明细

**ui.py**:
1. L709-711: `AwarenessReplyWorker.run()` 传 `save_to_session=False`（+3 行）
2. L9659-9689: `_stop_background_threads()` 新增 awareness_reply_threads 收集（+33 行，含错误处理）

**core/ai_client.py**:
1. L1973: `send_message()` 新增 `save_to_session=True` 参数
2. L2070-2075: 条件性追加 `request_message` 和 `auto_remember`
3. L2087-2093: 新增 `context_messages` 临时列表逻辑
4. L2108-2121: 条件性追加回复消息和保存会话

### 未修改内容

- `core/desktop_awareness.py` — 无改动
- `core/desktop_vision.py` — 无改动
- UI 布局、样式 — 无改动
- 普通聊天功能 — 无改动
- 语音功能 — 无改动
- 多会话管理 — 无改动
- SettingsDialog 之前修复 — 已保留

---

## 六、结论

三项风险全部处理完毕：
- **风险一**：已有充分防护，无需修改
- **风险二**：最小修复（+33 行），纳入统一关闭流程
- **风险三**：最小修复（+10 行），新增 `save_to_session` 参数隔离感知请求

所有自动化测试通过。建议在真实桌面环境中完成 GUI 交互验收后进入下一阶段。
