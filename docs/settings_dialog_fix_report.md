# SettingsDialog Bug 修复报告

**提交基线**: `5cfda5b`
**分支**: `fix/awareness-thread-safety-and-reply`
**日期**: 2026-10-18
**修改文件**: `ui.py` (+9 行, -2 行)

---

## 一、问题定位

### 症状
打开设置对话框时触发 `AttributeError`:
```
AttributeError: 'SettingsDialog' object has no attribute '_on_awareness_toggled'
```

### 根因
`ui.py` L2353 中，`SettingsDialog._build_scroll_content()` 将感知开关的信号连接到 `self._on_awareness_toggled`：
```python
self.awareness_enabled_checkbox.toggled.connect(
    self._on_awareness_toggled
)
```
但 `_on_awareness_toggled` 仅定义在 `AronaUI` 类（L8190），`SettingsDialog` 中不存在该方法。

### 设计背景
`SettingsDialog` 是独立的对话框类，不持有感知引擎（`desktop_awareness` 属于 `AronaUI`）。通过信号机制通信是正确的设计模式。

---

## 二、修复方案

### 改动 1: `SettingsDialog` 新增信号（L723）
```python
awareness_toggled = Signal(bool)
```

### 改动 2: 修正信号连接（L2353-2356）
```python
# 旧：直接调用不存在的方法
self.awareness_enabled_checkbox.toggled.connect(
    self._on_awareness_toggled
)
# 新：发射信号给主窗口处理
self.awareness_enabled_checkbox.toggled.connect(
    lambda checked: self.awareness_toggled.emit(checked)
)
```

### 改动 3: `open_settings` 中连接并同步状态（L7381-7393）
```python
dialog.awareness_toggled.connect(
    self._on_awareness_toggled
)
if hasattr(dialog, "awareness_enabled_checkbox"):
    aw_settings = self.ai_client.settings.settings.get("desktop_awareness", {})
    dialog.awareness_enabled_checkbox.setChecked(
        bool(aw_settings.get("enabled", False))
    )
```

---

## 三、验证结果

| 测试 | 结果 |
|------|------|
| py_compile (ui.py, desktop_vision.py, desktop_awareness.py) | ✅ 3/3 PASS |
| SettingsDialog 创建（不再 AttributeError） | ✅ PASS |
| Qt 线程安全（temporarily_hidden） | ✅ PASS |
| 感知引擎生命周期 | ✅ PASS |
| WorkerToken 隔离 | ✅ PASS |
| 线程管理（差异=0） | ✅ PASS |
| 安全回归 | ✅ 10/10 PASS |

---

## 四、GUI 验收状态

| 项目 | 状态 |
|------|------|
| 设置对话框打开/关闭 | ✅ 已验证（Python 脚本） |
| 感知开关切换 | ⏳ 需人工确认（需在 GUI 中操作） |
| 状态同步（保存后重新打开） | ⏳ 需人工确认 |
| 无副作用（其他功能） | ⏳ 需人工确认 |

**说明**: 由于当前测试环境为 headless 模式，无法执行完整 GUI 交互测试。建议在真实桌面环境中：
1. 打开 AronaClient
2. 点击设置按钮（⚙️）
3. 确认对话框正常打开（不再有崩溃）
4. 切换"启用桌面自主感知"开关
5. 点击保存 → 关闭 → 重新打开，确认开关状态一致

---

## 五、未修改内容

- `core/desktop_vision.py` — 未修改
- `core/desktop_awareness.py` — 未修改
- UI 布局、样式 — 未修改
- 其他功能逻辑 — 未修改
- 已知的三项风险（停止竞态、线程清理、会话隔离）— 本轮未处理

---

## 六、建议

**本轮修复通过。** 建议在真实 GUI 环境中完成人工验收后，进入下一阶段修复三项已知风险。
