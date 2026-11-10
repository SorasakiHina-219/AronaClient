# AronaClient

AronaClient 是一个面向 Windows 的桌面 AI 伙伴程序，基于 PySide6 构建，提供对话、语音识别、3D 角色渲染与空间化界面。

> **开发状态：早期开发中。** 功能与接口随时可能变更。下文严格区分「已实现 / 部分实现 / 未实现」，请以代码为准。

---

## 运行状态（请先读）

**当前分支（`feat/desktop-control-v0.2-phase1`）的已跟踪代码无法从干净检出直接运行。**

实测（对 `HEAD` 的 `git archive` 干净检出执行导入）：

```
>>> import computer
ModuleNotFoundError: No module named 'computer.screen_capture'
```

触发链：`ui.py:65` → `core/ai_client.py:21` → `core/desktop_vision.py:85` → `computer/__init__.py:14`

原因：以下模块存在于开发者的本地工作副本，但**未纳入版本控制**：

| 模块 | 影响的包 |
|---|---|
| `computer/desktop_access.py` | `computer`（包初始化即失败） |
| `computer/screen_capture.py` | `computer` |
| `computer/window_info.py` | `computer` |
| `agent/authchannel.py` | `agent.child`、`agent.launcher` |
| `agent/crypto.py` | `agent.child`、`agent.launcher` |
| `agent/protocol.py` | `agent.child`、`agent.launcher` |
| `agent/winjob.py` | `agent.launcher` |

**注意：报错会被首个失败遮蔽。** 上表 7 个文件不会逐个报错——导入在第一个缺失处即中断（`computer/__init__.py:14` 的 `screen_capture`、`agent/child.py` 的 `authchannel`），其余 5 个文件根本不会被执行到。实测穷举导入全部 79 个已跟踪模块，只会看到 **2 个**不同的 `ModuleNotFoundError` 目标。因此不能以"只报 1 个错"推断"只缺 1 个文件"，补齐时需逐个验证。

**这 7 个模块没有公开获取渠道。** 它们不在本仓库、不在任何发布物内，本 README 无法提供下载或获取方式；需要这些功能请联系仓库所有者。

**结论**：包含上述 7 个文件的本地工作副本可以运行；仅克隆本仓库不能运行。这不影响下表中「已实现」的判定——它们指代码实现完整，实际可运行性受上述缺件限制。

干净检出下可独立导入的模块（实测）：`core.speech`、`core.memory`、`core.conversation`、`core.settings`、`core.message_renderer`、`core.image_generation`、`core.media_generation`、`services.app_context`、`runtime.model3d.asset_provider`、`spatial_ui`。

> 其中 `spatial_ui` 仅指**包能导入**：`import spatial_ui` 成功，但 `import spatial_ui.spatial_main_window` 与 `python -m spatial_ui` 同样报 `computer.screen_capture`（实测 exit=1）。**L2 界面在当前仓库状态下无法启动。**

---

## 已实现

代码已入库且实现完整。

| 能力 | 代码位置 | 说明 |
|---|---|---|
| AI 对话 | `core/ai_client.py` | 支持 `deepseek` 与 `openrouter` 两种 Provider；支持图片 / 视频 / 文件附件；含电脑任务计划的人工批准与撤销逻辑 |
| 多会话与历史 | `core/conversation.py`、`core/chat_manager.py` | 会话创建、切换、历史持久化 |
| 消息渲染 | `core/message_renderer.py` | Markdown 渲染（依赖 `Markdown`） |
| 设置与密钥 | `core/settings.py`、`core/settings_dialog.py` | 配置读写；API Key 只存入系统密钥环（`keyring`），不写入配置文件 |
| 语音识别（麦克风 STT） | `core/speech.py` | 麦克风录音 → 识别为文本填入输入框，**不自动发送**；识别经 Google Web Speech 接口，需联网 |
| 云端语音合成（TTS） | `core/media_generation.py`、`ui.py` | 经 OpenAI 兼容 `POST /audio/speech` 合成，结果保存为音频文件；入口：工具栏 🎙 →「🔊 语音合成（输入框文本）」。**默认关闭**，须先配置 `media_services.voice` 服务商，见「语音能力分级」 |
| 3D 角色渲染 | `mascot_3d.py`、`pmx_model.py`、`mascot_window.py`、`runtime/model3d/` | PMX 模型解析与 OpenGL 渲染、LBS 蒙皮 |
| 空间化界面（L2） | `spatial_ui/`（18 个文件） | 独立空间 UI；**需先补齐缺失模块才能启动**，见「运行状态」 |
| 本地记忆 | `core/memory.py` | JSON 键值存储 |
| 图像 / 媒体生成 | `core/image_generation.py`、`core/media_generation.py` | OpenAI 兼容接口客户端：图片、视频、语音（`/audio/speech`、`/audio/transcriptions`）；**不含内置 TTS 引擎** |
| 共享上下文 | `services/app_context.py` | L1/L2 共用同一 `AIClient` 与 `SettingsManager` |

## 部分实现

| 能力 | 现状 | 缺口 |
|---|---|---|
| 桌面感知 / 视觉 | `core/desktop_awareness.py`、`core/desktop_vision.py` 已入库；有独立授权开关 `settings.desktop_vision.enabled` | 底层 `computer/desktop_access.py`、`computer/screen_capture.py`、`computer/window_info.py` 未入库 → 实际不可用 |
| 电脑操作 | `computer/app_launcher.py`、`computer/downloader.py`、`computer/web_browser.py`、`computer/tool_registry.py` 已入库；应用启动含系统目录拦截、下载含安全校验 | `computer` 包初始化依赖未入库模块 → 当前无法导入 |
| Agent 子进程 | `agent/__init__.py`、`agent/__main__.py`、`agent/child.py`、`agent/launcher.py` 已入库 | `agent.child` / `agent.launcher` 导入失败（缺 4 个模块） |
| 今日安排卡片 | `spatial_ui/cards.py` 的 `ScheduleCard` | 仅展示，**未接入**数据源，代码内已标注「不虚构数据」 |
| 语音 | 见下方「语音能力分级」 | 云端 TTS 与两种 STT 已实现；本地 TTS 与完整语音对话未实现 |

### 语音能力分级

语音是四件不同的事，不要混为一谈：

| 能力 | 状态 | 证据与启用条件 |
|---|---|---|
| **云端 TTS**（文本 → 语音） | **已实现** | `core/media_generation.py::synthesize_voice()` → OpenAI 兼容 `POST /audio/speech`；入口 `ui.py:5209` 工具栏 🎙 → `open_voice_menu` →「🔊 语音合成（输入框文本）」→ `_voice_synthesize` → `_run_media_action("voice")`。输出为音频文件。<br>**默认关闭**，需同时满足：① `media_services.voice.enabled = true`；② `media_services.voice.provider` 指向某服务商；③ 该服务商配好 `base_url`、`model`、`protocol`（默认 `openai-audio`）；④ API Key 已存入 keyring。协议或密钥缺失即 fail-closed |
| **本地 TTS**（本机离线合成） | **未实现** | 全仓无 `pyttsx3` / `QTextToSpeech` / `gTTS`；不联网就无法合成 |
| **STT**（语音 → 文本） | **已实现**（两条通道） | ① 麦克风实时识别：`core/speech.py::SpeechRecognizer` → `recognize_google`（需联网），结果只填入输入框（`ui.py::speech_finished`）；② 音频文件识别：工具栏 🎙 →「🎙 语音识别（选择音频文件）」→ `transcribe_voice()` → `POST /audio/transcriptions`，与云端 TTS 受同一套 `media_services.voice` 门禁 |
| **完整语音对话**（STT→LLM→TTS 自动链路） | **未实现** | `core/media_generation.py::voice_conversation()` 仅为预留接缝，调用即抛 `unsupported_protocol`。目前只能手动分两步：先识别得文本、再单独触发合成 |

> 设置里的「启用语音输入」对应 `settings.voice.enabled`（默认 `true`），**目前只被设置对话框读写，不参与上述任何门禁**；TTS / STT 的真实门禁是 `media_services.voice.*`。
> 另外无音量设置；`voice` / `response_format` / `speed` 可通过服务商的 `extra_params` 配置（`speed` 默认 1.0）。

## 未实现

以下能力**当前没有任何实现**，请勿据此评估本软件：

- **流式输出**：`core/ai_client.py` 无 `stream` / `chunk` / `delta` / `yield` 实现，回复为一次性返回，界面无逐字显示。
- **本地 TTS 引擎（离线合成）**：无 `pyttsx3` / `QTextToSpeech` / `gTTS`，不联网无法合成；合成只能走已配置的云端服务商。无音量设置。
- **完整语音对话（STT → LLM → TTS）**：`core/media_generation.py::voice_conversation()` 为预留接缝，调用即抛 `unsupported_protocol`；当前只能手动分步（先识别、再单独合成）。
- **音乐播放**：无播放器实现（`pygame.mixer`、`QMediaPlayer`、`playsound` 等均无命中）。
- **权限分级体系**：无 L0–L3 分级、无 `PermissionService`、无独立执行记录/审计、无通用撤销面板。（已有的分级不是权限等级：当前仅存在「桌面视觉」单一授权开关，以及电脑任务计划的人工批准与撤销。）
- **自主 / 主动行为**：无建议引擎、无触发调度、无频率控制。
- **免打扰 / 专注模式**：无实现。
- **角色动画**：无表情 morph（`pmx_model.py` 完全不解析 morph）、无口型、无 IK、无待机/转身动画；当前为静态站姿与整体旋转。
- **环境层**：无房间、家具、灯光、窗景；空间背景为渐变图层（`spatial_ui/model_stage.py` 注明相关层已删除）。
- **提醒 / 待办执行**：无调度器、无持久任务队列。

---

## 安装要求

- **操作系统**：Windows 10 / 11（仅 Windows 实测；其他平台未验证）
- **Python**：3.12（开发与实测环境）
- **显卡**：支持 OpenGL（3D 角色渲染依赖）
- **网络**：需要访问 AI Provider 的 API

安装依赖：

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

依赖清单见 `requirements.txt`：`PySide6`、`openai`、`PyAutoGUI`、`Pillow`、`opencv-python`、`numpy`、`keyring`、`python-dotenv`、`Markdown`、`SpeechRecognition`、`urllib3`。

## 使用说明

1. 复制环境变量模板并填入密钥：

   ```bash
   copy .env.example .env
   ```

2. 在 `.env` 中配置：

   | 变量 | 说明 |
   |---|---|
   | `DEEPSEEK_API_KEY` | DeepSeek API Key |
   | `DEEPSEEK_MODEL` | 默认 `deepseek-chat` |
   | `OPENROUTER_API_KEY` | OpenRouter API Key（可选） |
   | `OPENROUTER_MODEL` | 默认 `deepseek/deepseek-chat` |
   | `PROVIDER` | `deepseek` 或 `openrouter` |

3. 启动：

   ```bash
   python main.py         # L1 主界面
   python -m spatial_ui   # L2 空间界面
   ```

> 提醒：如上文「运行状态」所述，仅克隆仓库而不补齐缺失模块时，启动会失败。

## 目录结构

```
AronaClient/
├── main.py              # L1 启动入口
├── ui.py                # L1 主界面
├── spatial_ui/          # L2 空间界面（python -m spatial_ui）
├── core/                # AI 客户端、会话、设置、感知、语音、生成
├── services/            # 共享上下文（L1/L2 共用单一 AIClient）
├── runtime/model3d/     # 3D 资源提供与校验
├── computer/            # 应用启动、下载、浏览器、工具注册
├── agent/               # Agent 子进程骨架
├── assets/              # 资源
├── tests/               # 测试
└── docs/                # 开发与审计文档
```

## 项目限制

- **干净检出不可运行**：缺少 7 个未入库模块，见上。
- 仅 Windows 实测，Linux / macOS 未验证。
- 3D 渲染依赖 OpenGL，驱动不满足时无法显示角色。
- 依赖外部 AI API，离线不可用；无本地模型回退。
- 语音合成依赖云端服务商且默认关闭；本机无离线 TTS。语音交互为分步操作（先识别得文本、再单独合成），没有自动的语音对话链路。
- 桌面视觉与电脑操作在补齐模块前不可用；即使补齐，也需用户显式授权。
- 早期开发版本，不提供稳定性或兼容性保证。

## 许可证

本项目的开源发布版本采用 **GNU General Public License v3.0（GPL-3.0）**，第三方组件声明见 `NOTICES`。

> 说明：`LICENSE` 与 `NOTICES` 只存在于开源发布分支；**当前分支尚未包含这两个文件**，因此此处不做文件链接。

## 贡献

欢迎提交 Issue 与 Pull Request。提交前请确保 `pytest tests/` 通过，并遵循 PEP 8。

---

**AronaClient** — 桌面 AI 伙伴（开发中）
