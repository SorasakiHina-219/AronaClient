# AronaClient 提交 5cfda5b — GUI 验收准备与风险复核报告

**当前分支**: `fix/awareness-thread-safety-and-reply`  
**当前提交**: `5cfda5b`  
**基线提交**: `334dccf`  
**复核方式**: 纯源码静态分析 + 环境状态检查  
**日期**: 2026-07-10  

---

## 零、环境状态确认

```
当前分支:     fix/awareness-thread-safety-and-reply
HEAD:         5cfda5b539985fba47c2d21ea79b96a30dc7fe94
工作区状态:   干净（无未提交源码改动）
未跟踪文件:   docs/*.md + 2 个测试脚本（非源码）
原分支状态:
  fix/desktop-awareness-freeze:   334dccf（未受影响）
  fix/pre-merge-acceptance:       7dcbff7（未受影响）
安全回归测试: 10/10 PASS
py_compile:  3/3 PASS
```

**结论**: 工作区干净，本次复核不会影响任何已有修改。

---

## 一、已验证事实（源码层面确认）

### 1.1 Fix A — Qt 线程安全 ✅ 源码确认

| 项目 | 结论 | 证据 |
|------|------|------|
| `_is_main_thread()` | 实现正确 | 使用 `QCoreApplication.instance().thread()` 身份比较，异常兜底返回 True |
| `_invoke_on_main_thread()` | 实现正确 | 主线程直接调用，跨线程用 `QMetaObject.invokeMethod(Qt.QueuedConnection)` |
| `temporarily_hidden()` 所有 GUI 操作 | 全部路由到主线程 | hide/show/setWindowState/raise_/activateWindow 均经 `_invoke_on_main_thread()` |
| `screen.grabWindow(0)` 位置 | 在主线程调用路径内 | `_do_capture` 被 `capture()` 调用，`temporarily_hidden` context manager 内执行 |
| `try/finally` 窗口恢复 | 异常安全 | 无论正常/yield返回/异常抛出，finally 块都会执行恢复逻辑 |
| `_settle()` 实现 | 使用 `time.sleep(0.02)` 循环，**不调用** `processEvents()` | 源码 L124-143 确认 |
| `DEFAULT_SETTLE_SECONDS` | **0.40 秒**（非 0.02 秒） | 源码 L49 确认；0.02 是单次 sleep 间隔，总等待时间为 0.4s |
| `Qt.WindowState` 枚举传递 | **无法从源码确认** | PySide6 对核心枚举的 meta-type 注册是黑盒，需运行时验证 |

### 1.2 Fix B — 主动 AI 回复 ✅ 源码确认

| 项目 | 结论 | 证据 |
|------|------|------|
| `AwarenessReplyWorker` 信号模式 | 与 `AIWorker` 完全一致 | finished/error Signal(str)，`@Slot()` run() 方法 |
| `_awareness_reply_busy` 设置 | L8079 设置为 True | |
| `_awareness_reply_busy` 释放路径 1 | L8116 `_on_awareness_reply_finished` 重置为 False | |
| `_awareness_reply_busy` 释放路径 2 | L8129 `_on_awareness_reply_error` 重置为 False | |
| 线程生命周期管理 | 与 `DesktopVisionWorker` 模式一致 | append → finished connect deleteLater+quit+remove |
| `_remove_awareness_reply_thread` | list.remove() 异常安全（不在 list 中则跳过） | |
| **`_on_awareness_alert` 无 `is_stopped` 检查** | 🔴 源码确认 | L8028-8070 中无 stopped/paused 状态检查 |
| **`_stop_background_threads` 不包含 `awareness_reply_threads`** | 🔴 源码确认 | L9628-9642 只遍历 `worker_threads`，未包含 `awareness_reply_threads` |
| **`closeEvent` 未清理 `awareness_reply_threads`** | 🔴 源码确认 | L9795 只调 `_stop_desktop_awareness()` + `_stop_background_threads()` |
| **`send_message` 共享 `self.messages`** | 🔴 源码确认 | L2069 append user msg → L2096 append assistant msg → L2121 `_save_current_session()` |
| **`_conversation_messages` 不过滤感知消息** | 🔴 源码确认 | 只过滤非 str 内容和非 user/assistant role，感知消息（含 `[桌面感知摘要]` 前缀）会通过过滤 |

### 1.3 Fix C — 诊断日志 ✅ 源码确认

| 项目 | 结论 | 证据 |
|------|------|------|
| 日志级别 | desktop_awareness: INFO 20次, WARNING 8次, DEBUG 1次 | 源码统计 |
| ui.py 日志 | INFO 5次, WARNING 2次 | 源码统计 |
| 截断保护 | description[:80], error[:100], text[:80] | 源码确认 |
| **日志输出目标** | **stderr（无 FileHandler）** | main.py 无 logging 配置，logger 使用默认 StreamHandler |
| **日志文件位置** | **无专用日志文件** | data/logs/ 下只有 mascot_shutdown.log，感知日志仅在控制台输出 |

---

## 二、尚存风险

### 🔴 风险 1：停止感知后仍可能提交 AI 请求（低概率但存在）

**源码位置**: `ui.py` L8028-8070（`_on_awareness_alert`）

**问题**: `_on_awareness_alert` 没有检查 `self.desktop_awareness.is_stopped`。守卫逻辑在 `DesktopAwareness._on_event_from_worker()` 中（L828-833），如果延迟信号在守卫检查之前通过了（极小概率的竞态），AI 请求仍会被提交。

**触发条件**:
1. 调用 `stop()` → `_state = STOPPED`
2. 旧 worker 线程的 `event_ready.emit(event)` 已在 Qt 队列中
3. 信号被 `_on_event_from_worker` 接收并**通过守卫**（因为 state 尚未完全更新）
4. `_on_alert(event)` 被调用 → `_on_awareness_alert` → 提交 AI 请求

**实际影响**: 极低概率，但理论上存在。日志中应能观察到 `"事件被丢弃（已停止）"` 或 `"提交 AI 回复请求"`。

**建议最小修复**: 在 `_on_awareness_alert` 入口加一行检查：
```python
if self.desktop_awareness and self.desktop_awareness.state.is_stopped:
    return
```

---

### 🔴 风险 2：窗口关闭时 `awareness_reply_threads` 未清理（中等）

**源码位置**: `ui.py` L9604-9762（`_stop_background_threads`）+ L9766-9839（`closeEvent`）

**问题**: `_stop_background_threads` 只遍历 `self.worker_threads`，不包含 `self.awareness_reply_threads`。窗口关闭时，如果有一个 AI 回复请求正在网络请求中，该线程不会被关闭。

**触发条件**: 关闭窗口时恰好有感知 AI 回复请求正在进行中。

**实际影响**: 线程泄漏（进程退出后 Python GC 会回收），但可能导致：
- 进程退出后仍有后台网络请求
- 退出日志报告中不显示该线程状态

**建议最小修复**: 在 `_stop_background_threads` 的 targets 收集阶段增加：
```python
for pair in getattr(self, "awareness_reply_threads", []):
    if isinstance(pair, (tuple, list)) and len(pair) >= 2:
        targets.append(("awareness_reply", pair[0]))
```

---

### 🟡 风险 3：感知回复污染用户会话历史（低-中）

**源码位置**: `core/ai_client.py` L2069, L2096, L2121

**问题**: `AwarenessReplyWorker.run()` 调用 `self.ai_client.send_message()`，该方法会：
1. 将感知消息追加到 `self.messages`（L2069）
2. 将 AI 回复追加到 `self.messages`（L2096）
3. 调用 `_save_current_session()` 保存到数据库（L2121）
4. 下次用户发消息时，`_conversation_messages()` 会将感知消息也作为上下文发送给 API

**触发条件**: 感知引擎运行期间，用户发送了任意消息。

**实际影响**:
- 感知消息（以 `[桌面感知摘要]` 开头）会出现在聊天历史中
- 后续 API 调用会包含感知对话作为上下文
- 可能消耗更多 tokens，影响响应质量和成本
- **不会导致崩溃或功能错误**

**建议最小修复**: 有两种方案：
- **方案 A**: 使用独立的 `AIClient` 实例（不共享 `messages`），仅用于感知回复
- **方案 B**: 在 `_conversation_messages` 中过滤掉包含 `[桌面感知摘要]` 的消息

---

### 🟡 风险 4：`setWindowState` 枚举传递兼容性（低）

**源码位置**: `core/desktop_vision.py` L184-188

**问题**: `QMetaObject.invokeMethod` 通过 `*args` 传递 `Qt.WindowState` 枚举值。Qt 的 meta-type 系统需要枚举已注册才能跨线程传递。

**实际影响**: PySide6 通常自动注册核心 Qt 枚举，概率上大概率正常。但如果出现恢复后窗口状态不正确（如最大化窗口恢复为普通窗口），可能是此原因。

**运行时验证**: 测试 C 中观察窗口恢复后状态。

---

### 🟢 信息项：日志无持久化

**问题**: `main.py` 未配置任何 FileHandler，`logger = logging.getLogger(__name__)` 使用 Python 默认 StreamHandler（输出到 stderr/控制台）。

**影响**: 感知日志只出现在应用运行时的控制台输出中，应用关闭后不可追溯。`data/logs/` 下没有感知相关的日志文件。

**建议**: 验收时通过控制台捕获输出，或使用 `python main.py > awareness_test.log 2>&1` 重定向。

---

## 三、无法仅凭源码确认的项目

| 项目 | 需运行时验证 | 理由 |
|------|-------------|------|
| `setWindowState` 枚举通过 invokeMethod 传递是否正确 | 测试 C | Qt meta-type 注册是运行时行为 |
| `_settle(0.40s)` 是否足够让 queued hide 操作完成 | 测试 A | 取决于系统负载和 event loop 调度 |
| `screen.grabWindow(0)` 在 worker 线程中的行为 | 测试 C | Windows DWM 跨线程 grab 行为 |
| 窗口销毁后 queued GUI 操作的异常处理 | 测试 A | 需要窗口在 hide 排队后、执行前被销毁 |
| 感知线程与主线程的资源竞争 | 测试 A/B | 高负载场景下才可能触发 |

---

## 四、人工 GUI 验收步骤

### 前置准备

1. **确保感知功能已配置**: 在设置中开启「桌面视觉」和「桌面自主感知」开关
2. **确保 AI API Key 已配置**: 否则无法测试 AI 回复
3. **日志捕获**: 启动时用重定向保存控制台输出：
   ```
   .\.venv\Scripts\python.exe main.py > awareness_test.log 2>&1
   ```
4. **关键日志关键词**（在日志文件中搜索）:
   - `桌面感知：触发提醒` — 感知事件到达
   - `桌面感知：提交 AI 回复请求` — AI 回复开始
   - `桌面感知：AI 回复完成` — AI 回复成功
   - `桌面感知：AI 回复失败` — AI 回复失败
   - `桌面感知：事件被丢弃（已停止）` — 停止后事件被正确丢弃
   - `桌面感知：事件被丢弃（旧 worker 延迟）` — 旧线程事件被隔离
   - `桌面感知：截图成功` / `截图失败` — 截图状态

---

### 测试 A：设置对话框不冻结

**前置条件**: 感知引擎正在运行（状态栏显示"桌面感知活跃"）

**操作步骤**:
1. 打开 AronaClient，确认感知引擎已启动
2. 打开设置对话框
3. 在设置对话框**保持打开**的状态下：
   - 切换「桌面自主感知」开关为关闭 → 等待 5 秒
   - 切换回开启 → 等待 30 秒（等一个完整 cycle）
4. 关闭设置对话框
5. 重复步骤 2-4，共 **3 轮**

**预期现象**:
- 设置对话框正常弹出/关闭，无卡顿
- 状态栏在开关切换后正确更新
- 日志中无冻结相关错误
- 任务管理器中感知线程数稳定（无持续增长）

**失败现象**:
- 界面卡死 > 3 秒无响应
- 设置对话框无法关闭
- 状态栏显示异常或无更新
- 日志中出现 `RuntimeError` 或 `QObject` 相关错误

**保存证据**: 
- 每轮开关切换后的状态栏截图
- 任务管理器线程数截图（开始前和结束后各一次）
- `awareness_test.log` 中相关条目

---

### 测试 B：开关切换无线程堆积

**前置条件**: 感知引擎正在运行

**操作步骤**:
1. 记录当前 python.exe 线程数（任务管理器 → 详细信息 → Threads 列）
2. 关闭感知开关 → 等待 5 秒
3. 重新开启感知开关 → 等待 35 秒（至少一个完整 cycle + 去重窗口）
4. 记录线程数
5. 重复步骤 2-4，共 **3 轮**

**预期现象**:
- 线程数在每轮切换前后波动 ≤ 2 个
- 无新 python.exe 进程产生
- 日志中每轮有明确的 "已停止" / "启动" 条目

**失败现象**:
- 线程数逐轮递增（> 每轮 +2）
- 出现多个 python.exe 进程
- 日志中出现 "线程未在 2000ms 内退出" 警告且逐轮累积

**保存证据**:
- 每轮前后的线程数截图
- 日志中 "已停止" / "启动" 条目

---

### 测试 C：静态通知与 AI 回复均正确显示

**前置条件**: 感知引擎运行中，API Key 已配置

**操作步骤**:
1. 清空聊天或确认当前为新对话
2. 切换到其他程序窗口（如浏览器），制造画面变化
3. 等待最多 90 秒（30s 间隔 × 3 轮去重）
4. 观察聊天界面

**预期现象**:
- 出现 `[桌面感知] xxx` 系统通知消息（灰色/不同样式）
- 约 5-15 秒后出现 AI 生成的回复消息（无 `[桌面感知]` 前缀）
- 状态栏短暂显示 "桌面感知：Arona 正在思考……" 后恢复
- 日志中有 "触发提醒" → "提交 AI 回复请求" → "AI 回复完成" 顺序条目

**失败现象**:
- 只有静态通知，无 AI 回复（检查日志是否有 "提交 AI 回复请求"）
- AI 回复显示为错误信息
- 聊天界面卡顿
- 日志中 "AI 回复失败" 条目

**保存证据**:
- 聊天记录截图（需包含两条消息）
- 日志中完整的事件链

---

### 测试 D：停止感知后无新 AI 请求

**前置条件**: 感知引擎已运行，已触发过至少一次 AI 回复

**操作步骤**:
1. 记录当前聊天中最后的消息时间
2. 关闭感知开关
3. **立即**切换窗口制造变化
4. 等待 60 秒
5. 观察聊天界面和状态栏

**预期现象**:
- 停止后不再出现新的 `[桌面感知]` 消息
- 状态栏显示 "桌面感知已停止" 或类似状态
- 日志中有 "事件被丢弃（已停止）" 条目
- **注意**: 如果在停止前有 "提交 AI 回复请求" 日志，对应的 AI 回复可能在停止后仍会显示（这是正常行为，是停止前已提交的请求）

**失败现象**:
- 停止后仍出现新的 `[桌面感知]` 消息
- 停止后仍出现 AI 生成回复（且日志中无 "提交 AI 回复请求" 在停止前）
- 状态栏仍显示 "活跃"

**保存证据**:
- 停止前后的聊天记录截图
- 日志中 "已停止" + "事件被丢弃" 条目

---

### 测试 E：普通聊天与感知回复共存不冲突

**前置条件**: 感知引擎运行中，API Key 已配置

**操作步骤**:
1. 在聊天中输入一条普通消息并发送（如 "你好"）
2. 在 Arona 回复**过程中**（状态栏显示 "正在思考"），切换到其他窗口制造感知变化
3. 等待两条回复都完成
4. 观察消息顺序和内容

**预期现象**:
- 普通聊天消息和感知回复各自独立显示
- 消息顺序按时间排列（无交叉混乱）
- 输入框可正常输入新消息
- 状态栏正确反映当前状态

**失败现象**:
- 感知回复插入到普通聊天中间
- 输入框不可用或卡死
- 消息内容显示异常（如乱码、空白）
- 两条回复内容混在一起

**保存证据**:
- 聊天记录截图（显示两条独立回复）
- 日志中对应时间戳的条目

---

## 五、额外建议验证项（可选）

### F. 窗口状态恢复验证
- 将 AronaClient 窗口**最大化** → 触发感知截图 → 观察窗口是否恢复为最大化
- 将窗口**最小化** → 触发感知截图 → 观察窗口是否恢复为最小化

### G. 多显示器验证
- 如果有多个显示器，切换主显示器 → 触发感知 → 观察截图是否来自正确显示器

### H. 长时间运行稳定性
- 保持感知运行 10+ 分钟，观察内存增长和线程数稳定性

---

## 六、失败时的日志收集清单

如果任何测试失败，请收集以下信息：

1. **控制台日志**: `awareness_test.log` 完整内容
2. **聊天记录截图**: 显示异常消息的部分
3. **状态栏截图**: 显示异常状态的部分
4. **任务管理器**: python.exe 的线程数和内存使用
5. **数据库内容**: `data/chats/` 目录下最近修改的 session 文件（确认会话污染情况）
6. **错误回溯**: 如果有任何异常信息或 traceback

---

## 七、建议的最小修复范围（如发现风险 1/2/3）

| 风险 | 文件 | 代码量 | 修改内容 |
|------|------|--------|----------|
| 风险 1: 停止后仍提交 | `ui.py` L8041 后 | +3 行 | 添加 `is_stopped` 检查 |
| 风险 2: 线程泄漏 | `ui.py` L9638 后 | +5 行 | 在 `_stop_background_threads` 中增加 `awareness_reply_threads` |
| 风险 3: 会话污染 | `core/ai_client.py` 或 `ui.py` | +5~15 行 | 过滤感知消息 或 使用独立 client |

以上修复均为局部追加，不触碰现有 UI 布局、样式和授权门�逻辑。

---

## 八、最终结论

| 维度 | 结论 |
|------|------|
| 源码正确性 | ✅ 核心逻辑正确，线程安全架构合理 |
| 安全风险 | 🔴 3 项需关注（见第二部分），均不影响基本功能，但建议在验收通过后修复 |
| 测试覆盖 | ⏸️ 自动化测试通过（10/10），GUI 测试待人工执行 |
| 进入下一阶段 | ✅ **可以进入人工 GUI 验收**。三项风险可在验收过程中观察确认，也可在验收后单独修复 |

---

**本复核基于源码静态分析，未执行任何 GUI 测试。五项人工验收步骤（A-E）需人工操作执行。详细风险说明和最小修复范围见本报告第二、七部分。**
