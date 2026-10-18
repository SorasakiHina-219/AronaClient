# AronaClient 桌面感知修复 — 交付报告

**分支**: `fix/awareness-thread-safety-and-reply`  
**提交**: `5cfda5b`  
**父提交**: `334dccf`（冻结修复）  
**日期**: 2026-07-10

---

## 一、变更文件清单

| 文件 | 变更 | 修复内容 |
|------|------|----------|
| `core/desktop_vision.py` | +80/-10 | Fix A: 线程安全窗口管理 |
| `core/desktop_awareness.py` | +90/-14 | Fix C: 诊断日志 |
| `ui.py` | +140/-3 | Fix B: 主动 AI 回复 |
| **合计** | **+286/-24** | |

---

## 二、修复详情

### Fix A：Qt 线程安全与卡顿消除

**问题**: `temporarily_hidden()` 在 worker 线程中直接调用 `w.hide()/show()/raise_/activateWindow()/setWindowState()`，这些是 Qt GUI 操作，必须在主线程执行。当 SettingsDialog 的 `exec()` 阻塞主事件循环时，产生竞态冻结。

**修改**:
- 新增 `_is_main_thread()` — 检测当前是否在 Qt 主线程
- 新增 `_invoke_on_main_thread(obj, method, *args)` — 通过 `QMetaObject.invokeMethod` + `Qt.QueuedConnection` 将方法调用排队到主线程
- `temporarily_hidden()` 中所有 GUI 操作改用 `_invoke_on_main_thread()` 路由
- `screen.grabWindow(0)` 和 `get_active_window_info()` 保持原位（屏幕级/Win32 操作，不依赖 widget 状态）
- 更新相关 docstring

**保留行为**:
- 截图仍然隐藏 AronaClient 窗口（避免拍到 84% 的自己）
- `_settle()` 的 `time.sleep(0.02)` 循环不变
- 异常不会传播到调用方

### Fix B：主动 AI 回复

**问题**: `_on_awareness_alert()` 仅调用 `add_message("assistant", "[桌面感知] xxx")` 添加静态通知，未触发 AI 对话。

**修改**:
- 新增 `AwarenessReplyWorker` 类（位于 `ui.py` L672-708）
  - 复用现有 `AIClient.send_message()` 接口
  - 信号模式与 `AIWorker` 完全一致（finished/error）
  - 消息格式：`[桌面感知摘要]\n{description}\n\n请根据以上...`
- 修改 `_on_awareness_alert()`（`ui.py` L8032-8085）
  - 保留静态通知（带 `[桌面感知]` 前缀，可与 AI 回复区分）
  - 新增 AI 回复触发（通过 `_submit_awareness_reply()`）
  - 增加 `_awareness_reply_busy` 守卫防重复提交
- 新增线程管理方法
  - `_submit_awareness_reply()` — 创建 AwarenessReplyWorker + QThread
  - `_on_awareness_reply_finished()` — 显示 AI 回复
  - `_on_awareness_reply_error()` — 显示错误
  - `_remove_awareness_reply_thread()` — 清理已结束线程
- 新增初始化字段（`ui.py` L3991-3992）
  - `_awareness_reply_busy = False`
  - `awareness_reply_threads = []`

**不影响的范围**:
- 用户发送消息的 AI 回复路径完全不变
- `_ai_busy` 状态独立于 `_awareness_reply_busy`
- 静态通知 `[桌面感知]` 仍出现在聊天中

### Fix C：可诊断日志

**问题**: logging 默认 WARNING 级别，感知引擎所有关键日志是 DEBUG，运行时不可见。

**修改** (`desktop_awareness.py`):
- `_cycle()` 中各关键节点增加 INFO 级别日志：
  - 截图成功/失败（含分辨率、大小、耗时）
  - pHash 计算失败（WARNING）
  - 画面变化检测（含变化次数）
  - AI 分析完成/失败（含耗时）
  - 事件提交主线程（含总耗时）
- `_capture_screenshot()` 中 debug → info/warning
- `_analyze_shot()` 中 debug → info
- `_on_event_from_worker()` 守卫日志 debug → info
- 新增 `summary()` 方法返回引擎状态摘要

---

## 三、测试证据

### 3.1 语法检查

```
PASS: core/desktop_vision.py
PASS: core/desktop_awareness.py
PASS: ui.py
```

### 3.2 Import 测试

```
desktop_vision imports: OK
desktop_awareness imports: OK
Qt app created: OK
_is_main_thread(): True
测试通过
```

### 3.3 安全回归测试

```
Results: 10 passed, 0 failed
```

### 3.4 应用启动验证

```
启动 PID: 14092
应用仍在运行
  内存: 2.5 MB
  CPU: 0s
  线程: 3
```

---

## 四、未覆盖场景

以下场景需要人工 GUI 验证：

| 场景 | 验证方式 |
|------|----------|
| 感知运行时打开设置对话框，界面不卡死 | 手动操作 |
| 开关反复切换 3+ 次，无重复线程堆积 | 手动操作 + 任务管理器 |
| 感知事件触发 AI 回复并显示在聊天中 | 手动操作 |
| AI 回复失败时显示错误信息 | 断开网络后手动测试 |
| 停止感知后不再有 AI 回复请求 | 手动操作 |
| 与现有聊天功能共存不冲突 | 手动操作 |

---

## 五、分支与提交信息

```
当前分支: fix/awareness-thread-safety-and-reply
最新提交: 5cfda5b
父提交:   334dccf

原分支（未受影响）:
  fix/desktop-awareness-freeze: 334dccf
  fix/pre-merge-acceptance:     7dcbff7
```

---

## 六、下一步建议

1. **人工 GUI 验证**（5 项，见上方表格）
2. **验证通过后合并策略**:
   - 方案 A：将 `fix/awareness-thread-safety-and-reply` 合并到 `fix/desktop-awareness-freeze`，再一起合并到 master
   - 方案 B：分别验证后独立合并
3. **后续 Phase 1A**：在稳定基线上创建独立功能分支，开始统一操作授权框架开发

---

**修复完成。等待人工 GUI 验收。**
