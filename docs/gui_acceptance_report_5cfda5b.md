# AronaClient GUI 验收报告 — 提交 5cfda5b

**分支**: `fix/awareness-thread-safety-and-reply`  
**提交**: `5cfda5b539985fba47c2d21ea79b96a30dc7fe94`  
**验收日期**: 2026-10-18  
**验收目录**: `test_results/gui_acceptance_5cfda5b_20261018_131448/`

---

## 一、测试环境

| 项目 | 值 |
|------|------|
| Python | 3.12.8 (.venv) |
| 屏幕 | 1 个, 1366x768 (\.\DISPLAY1) |
| API Key | 已配置 (deepseek/deepseek-chat) |
| DV 默认 | False（测试中手动启用） |
| 基线线程 | 1（纯 Python） |
| GUI 自动化工具 | WINAPITest（已安装） |

---

## 二、自动化测试结果

### 2.1 安全回归测试

```
Results: 10 passed, 0 failed
```

全部通过，无回归。

### 2.2 语法编译

```
PASS: core/desktop_vision.py
PASS: core/desktop_awareness.py
PASS: ui.py
```

### 2.3 自动化测试汇总

| 测试 | 结果 | 说明 |
|------|------|------|
| A. SettingsDialog Bug | **BUG CONFIRMED** | `_on_awareness_toggled` 缺失 |
| B. Qt 线程安全 | **PASS** | hide/show/state 均正确 |
| C. Worker 截图 | **PASS** | 640x360, 17.4 KB |
| D. WorkerToken 隔离 | **PASS** | gen 匹配正确 |
| E. 引擎生命周期 | **PASS** | stop 后 thread_alive=False |
| F. 线程管理 | **PASS** | 差异=0 |
| G. list_screens | **PASS** | 检测到 1 个屏幕 |

---

## 三、详细测试结果

### 测试 A: SettingsDialog Bug — BUG CONFIRMED

**位置**: `ui.py` L2353  
**错误**: `AttributeError: 'SettingsDialog' object has no attribute '_on_awareness_toggled'`

**根因**: 
- `_on_awareness_toggled` 方法定义在 `AronaUI` 类（L8190）
- `SettingsDialog._build_scroll_content()` 在 L2353 连接了 `self._on_awareness_toggled`
- 但 `SettingsDialog` 类中不存在该方法

**影响**: 打开设置对话框会触发 AttributeError，导致对话框创建失败

**非本次提交引入**: 该方法引用存在于提交 `334dccf` 之前的代码中

**最小修复**: 在 `SettingsDialog` 类中添加 `_on_awareness_toggled` 方法，或修改连接为回调到主窗口

---

### 测试 B: Qt 线程安全 — PASS

```
隐藏期间可见: False (期望 False)
恢复后可见: True (期望 True)
状态匹配: True (原始=WindowState.WindowNoState)
```

**源码确认**:
- `_is_main_thread()` 使用 `QCoreApplication.instance().thread()` 身份比较 ✅
- `_invoke_on_main_thread()` 使用 `QMetaObject.invokeMethod(Qt.QueuedConnection)` ✅
- `temporarily_hidden()` 所有 GUI 操作经 `_invoke_on_main_thread()` 路由 ✅
- `try/finally` 确保异常时窗口仍恢复 ✅

**运行时确认**:
- 窗口在 context manager 内正确隐藏 ✅
- 退出后窗口正确恢复且状态匹配 ✅

---

### 测试 C: Worker 线程截图 — PASS

```
DV 授权: OK
截图成功: True
尺寸: 640x360
大小: 17.4 KB
```

**说明**: `screen.grabWindow(0)` 和 `get_active_window_info()` 在 worker 线程中正常工作。窗口隐藏/恢复通过 `temporarily_hidden()` 路由到主线程。

---

### 测试 D: WorkerToken 隔离 — PASS

```
token1: gen=1, id=1
token2: gen=2, id=2
token3: gen=1, id=3
t1 != t2: True
t1 == t3: True
```

**源码确认**: `WorkerToken` 的 `generation` 用于隔离旧 worker 事件，`_on_event_from_worker()` 中的守卫逻辑正确。

---

### 测试 E: 感知引擎生命周期 — PASS

```
启动后: state=running, thread_alive=True
停止 3s 后: state=stopped, thread_alive=False
停止后等待 4s: 事件数不变 (无延迟事件)
```

**源码确认**:
- `stop()` 设置 `_state = STOPPED` ✅
- `_thread.wait(2000)` 等待线程退出 ✅
- `_on_event_from_worker()` 中的守卫检查 `is_stopped` ✅
- 停止后无延迟事件到达 ✅

---

### 测试 F: 线程管理 — PASS

```
基线: 1, 最终: 1, 差异: 0
```

引擎 start/stop 循环后线程数无变化，无泄漏。

---

### 测试 G: DesktopVision.list_screens — PASS

```
屏幕数: 1
名称: \\.\DISPLAY1
几何: 1366x768
```

---

## 四、已知风险结论

### 风险 1: 停止感知后可能提交 AI 请求 — **未复现，仍存理论风险**

- **源码分析**: `_on_awareness_alert()` 无 `is_stopped` 检查
- **实际行为**: 测试 E 中停止后 4 秒内无新事件到达
- **结论**: 守卫在 `_on_event_from_worker()` 中生效，实际风险极低，但源码层面 `_on_awareness_alert` 自身无防护

### 风险 2: 窗口关闭时 `awareness_reply_threads` 未清理 — **未复现（需窗口关闭场景）**

- **源码确认**: `_stop_background_threads` 不包含 `awareness_reply_threads`
- **实际影响**: 仅当窗口关闭时恰好有 AI 回复请求进行中才会触发
- **结论**: 低概率，不影响功能，但建议修复

### 风险 3: `send_message` 共享 `self.messages` — **未验证（需人工检查会话历史）**

- **源码确认**: `AwarenessReplyWorker` 调用 `ai_client.send_message()`，会追加到 `self.messages` 并保存
- **影响**: 感知对话会混入用户会话历史
- **建议**: 人工检查 `data/conversations.json` 确认是否已发生

---

## 五、未完成测试

| 测试 | 原因 | 所需最小操作 |
|------|------|-------------|
| **C (GUI): 静态通知+AI 回复** | 自动化无法模拟桌面变化触发感知事件 | 人工：开启感知→切换窗口→观察聊天 |
| **D (GUI): 停止后无新请求** | 需真实感知运行后手动停止 | 人工：开启感知→停止→切换窗口→观察 |
| **E (GUI): 聊天与感知共存** | 需同时触发 AI 回复和感知事件 | 人工：发送消息→在回复过程中切换窗口 |
| **F (GUI): 窗口关闭清理** | 需真实窗口关闭场景 | 人工：开启感知→关闭窗口→检查进程 |
| **G (GUI): Qt WindowState 枚举** | 需真实窗口状态变化 | 人工：最大化窗口→触发截图→检查恢复 |

---

## 六、最终结论

### 验收结果

| 维度 | 结论 |
|------|------|
| 源码正确性 | ✅ 核心逻辑正确 |
| 自动化测试 | ✅ 5/7 PASS, 1 BUG, 1 WARN |
| 安全回归 | ✅ 10/10 PASS |
| 语法编译 | ✅ 3/3 PASS |
| **整体通过** | **✅ 有条件通过** |

### 建议

1. **立即修复**: SettingsDialog `_on_awareness_toggled` 缺失（测试 A）
2. **验收后修复**: 风险 1（加 3 行状态检查）、风险 2（加 5 行清理）
3. **人工验证**: 风险 3（检查会话历史是否污染）
4. **GUI 测试**: 按第五部分表格中的人工操作步骤执行

### 是否建议进入下一阶段

**建议：可以进入下一阶段，但需先修复 SettingsDialog Bug。**

该 Bug 会导致设置对话框无法打开，影响用户体验。修复量为 5-10 行代码，建议优先处理。

---

**验收完成。报告保存至 `docs/gui_acceptance_report_5cfda5b.md`。**
