"""
spatial_ui.spatial_main_window — L2 空间主窗口。

布局（对应参考图）：
    ┌─ 顶栏：Logo | 导航胶囊 | 时钟/状态/设置/头像 ─────────────┐
    │ 侧边栏 │            中央舞台 / 页面           │ 右侧信息列 │
    │ 档案卡 │  (Arona 空间角色 + 悬浮卡片)          │ 快速操作   │
    │ 导航   │                                      │ 今日安排   │
    │ 版本   │  [悬浮输入胶囊]                       │ 模型       │
    └────────┴──────────────────────────────────────┴───────────┘

约束：
  - 不修改 ui.py / core 业务逻辑
  - 聊天仍走原 AIClient 接口
  - 未接入功能明确标注，不虚构数据
  - low / medium / high 三档性能降级
"""

import warnings
warnings.filterwarnings("ignore")

import sys
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QThread, QObject,
    QPropertyAnimation, QEasingCurve, QAbstractAnimation, QPoint, QRect,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QStackedWidget,
    QScrollArea,
    QFrame,
    QPushButton,
    QDialog,
    QFormLayout,
    QLineEdit,
    QComboBox,
    QGroupBox,
    QToolButton,
)
from PySide6.QtGui import QGuiApplication

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ai_client import AIClient
from core.settings import SettingsManager

from spatial_ui.styles import (
    RECOMMENDED_WINDOW_WIDTH,
    RECOMMENDED_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    MIN_WINDOW_HEIGHT,
    SIDEBAR_EXPANDED_WIDTH,
    SIDEBAR_COLLAPSED_WIDTH,
    PANEL_MARGIN,
    PANEL_SPACING,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_DIM,
    COLOR_ACCENT_BLUE,
    COLOR_ACCENT_PINK,
    PERFORMANCE_MODES,
    PANEL_RADIUS,
    PANEL_KINDS,
    EDGE_HANDLE_W,
    EDGE_HANDLE_H,
    SIDEBAR_ANIM_MS,
    FADE_TOP_HEIGHT,
    FADE_BOTTOM_HEIGHT,
)
from spatial_ui.theme import build_app_qss
from spatial_ui.space_background import SpaceBackground
from spatial_ui.sidebar import SpatialSidebar
from spatial_ui.topbar import SpatialTopbar
from spatial_ui.composer import SpatialComposer
from spatial_ui.model_stage import SpatialModelStage
from spatial_ui.cards import (
    QuickActionsCard,
    SystemStatusCard,
    ScheduleCard,
    ModelInfoCard,
    GreetingBubble,
    InfoCard,
)
from spatial_ui.bubble import MessageBubble
from spatial_ui.glass_panel import GlassPanel
from spatial_ui.window_manager import SpatialWindowManager
from spatial_ui.floating_menu import QuickBar, ContextPanel
from spatial_ui.chat_fade import FadeOverlay

RIGHT_COLUMN_WIDTH = 268

# 空间 UI 自身的界面状态（侧栏显示状态等）。
# 单独放 data/ 下，避免改动 core/settings.py 的默认键过滤逻辑。
UI_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "spatial_ui_state.json"


def _load_ui_state() -> dict:
    try:
        if UI_STATE_FILE.exists():
            import json
            data = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_ui_state(data: dict):
    try:
        import json
        UI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = UI_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(UI_STATE_FILE)
    except Exception:
        pass


def _update_ui_state(**changes):
    data = _load_ui_state()
    data.update(changes)
    _save_ui_state(data)


# ──────────────────────────────────────────────────────────
# 首页：Arona 舞台 + 悬浮卡片
# ──────────────────────────────────────────────────────────

class HomePage(QWidget):
    """首页：中央角色舞台，四周悬浮信息卡。"""

    def __init__(
        self,
        *,
        performance_mode: str = "medium",
        enable_3d: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._perf_mode = performance_mode

        self.stage = SpatialModelStage(
            self,
            performance_mode=performance_mode,
            enable_runtime=enable_3d,
        )
        self.stage.runtime_state_changed.connect(self._on_runtime_state)

        self.greeting = GreetingBubble(
            parent=self, performance_mode=performance_mode
        )
        self.status_card = SystemStatusCard(
            parent=self, performance_mode=performance_mode
        )
        self.status_pill = self._make_status_pill()

        self._layout_children()

    def _make_status_pill(self) -> QLabel:
        label = QLabel("Arona · 待机中")
        label.setStyleSheet(
            "QLabel {"
            " background: rgba(26, 32, 60, 0.72);"
            " border: 1px solid rgba(150,172,255,0.22);"
            " border-radius: 12px;"
            f" color: {COLOR_TEXT_SECONDARY};"
            " font-size: 11px; padding: 5px 12px;"
            "}"
        )
        label.setParent(self)
        return label

    def _layout_children(self):
        m = 18
        w = self.width()
        h = self.height()

        self.stage.setGeometry(0, 0, w, h)

        # 左上状态胶囊
        self.status_pill.adjustSize()
        self.status_pill.move(m, m)

        # 左侧问候气泡
        self.greeting.setFixedWidth(min(300, max(220, int(w * 0.30))))
        self.greeting.adjustSize()
        gh = self.greeting.sizeHint().height()
        self.greeting.setGeometry(
            m, int(h * 0.46), self.greeting.width(), gh)

        # 左下系统状态
        self.status_card.setFixedWidth(min(230, max(190, int(w * 0.22))))
        self.status_card.adjustSize()
        sh = max(96, self.status_card.sizeHint().height())
        self.status_card.setGeometry(
            m, max(int(h * 0.5), h - sh - 96),
            self.status_card.width(), sh)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()

    def set_status_text(self, text: str):
        self.stage.set_status_text(text)
        self.status_pill.setText(text)
        self.status_pill.adjustSize()

    def _on_runtime_state(self, state: str):
        if state == "loading":
            self.set_status_text("正在加载 Arona 3D 模型…")
        elif state == "ready":
            self.set_status_text("Arona · 3D 模型已就绪")
        elif state == "failed":
            self.set_status_text("Arona · 3D 不可用（占位模式）")
        elif state in ("disabled", "off"):
            self.set_status_text("Arona · 待机中")

    def set_3d_enabled(self, enabled: bool):
        self.stage.enable_runtime(enabled)

    def reload_model(self):
        self.stage.reload_model()

    def reset_model_view(self):
        self.stage.reset_view()

    def set_greeting(self, title: str, sub: str):
        self.greeting.set_greeting(title, sub)

    def set_performance_mode(self, mode: str):
        self._perf_mode = mode
        self.stage.set_performance_mode(mode)
        self.status_card.set_performance_mode(mode)


# ──────────────────────────────────────────────────────────
# 占位页（未接入模块）：明确标注，不虚构内容
# ──────────────────────────────────────────────────────────

class PlaceholderPage(QWidget):
    """未接入模块的占位页。"""

    def __init__(self, icon: str, title: str, note: str, parent=None):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        card = GlassPanel(
            self, radius=PANEL_RADIUS, tint=(28, 34, 64), fill_alpha=150
        )
        card.setMaximumWidth(420)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 26, 28, 26)
        cl.setSpacing(10)

        ic = QLabel(icon)
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet(
            f"color: {COLOR_ACCENT_BLUE}; font-size: 34px;"
            f" background: transparent;"
        )

        t = QLabel(title)
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px;"
            f" font-weight: 700; background: transparent;"
        )

        n = QLabel(note)
        n.setAlignment(Qt.AlignCenter)
        n.setWordWrap(True)
        n.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
            f" background: transparent;"
        )

        cl.addWidget(ic)
        cl.addWidget(t)
        cl.addWidget(n)

        outer.addWidget(card, alignment=Qt.AlignHCenter)
        outer.addStretch(2)


# ──────────────────────────────────────────────────────────
# 主窗口
# ──────────────────────────────────────────────────────────

class SpatialMainWindow(QMainWindow):
    """L2 空间主窗口。"""

    performance_mode_changed = Signal(str)

    def __init__(
        self,
        performance_mode: str = "medium",
        *,
        ai_client=None,
        settings=None,
        window_manager=None,
        **_kwargs,
    ):
        super().__init__()

        self._performance_mode = performance_mode
        self._perf = PERFORMANCE_MODES.get(
            performance_mode, PERFORMANCE_MODES["medium"]
        )
        self._ai_busy = False

        # 业务核心（原样）
        # 依赖注入：预览模式可通过参数传入 mock 实例，
        # 不传则保持原有默认行为（正式 AIClient / SettingsManager）。
        self.ai_client = ai_client if ai_client is not None else AIClient()
        self.settings = settings if settings is not None else SettingsManager()
        self.window_manager = (
            window_manager
            if window_manager is not None
            else SpatialWindowManager(performance_mode=performance_mode)
        )

        # 应用级主题（关键：让对话框/消息框也变深色）
        self._apply_app_theme()

        self._setup_window()
        self._build_ui()
        self._connect_signals()
        self._restore_state()
        self._refresh_status()

        self.home_page.status_card.start_monitoring()

    # ── 主题 ──────────────────────────────────────────────

    def _apply_app_theme(self):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(
                build_app_qss(performance_mode=self._performance_mode)
            )
            app.setStyle("Fusion")

    # ── 窗口 ──────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowTitle("AronaClient · 空间交互界面")
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail:
            w = min(RECOMMENDED_WINDOW_WIDTH, avail.width() - 40)
            h = min(RECOMMENDED_WINDOW_HEIGHT, avail.height() - 40)
        else:
            w, h = RECOMMENDED_WINDOW_WIDTH, RECOMMENDED_WINDOW_HEIGHT
        self.resize(max(MIN_WINDOW_WIDTH, w), max(MIN_WINDOW_HEIGHT, h))

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

    # ── UI ────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("spatialCentral")
        central.setAttribute(Qt.WA_TranslucentBackground, True)
        central.setAutoFillBackground(False)
        self.setCentralWidget(central)

        # 空间背景（绝对填充）
        self.space_bg = SpaceBackground(
            self, performance_mode=self._performance_mode
        )
        self.space_bg.lower()
        self.space_bg.setGeometry(self.rect())

        root = QVBoxLayout(central)
        root.setContentsMargins(
            PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN
        )
        root.setSpacing(0)

        # 顶栏（通栏）
        self.topbar = SpatialTopbar(
            performance_mode=self._performance_mode
        )
        root.addWidget(self.topbar)

        # 主体
        body = QHBoxLayout()
        body.setContentsMargins(0, PANEL_SPACING, 0, 0)
        body.setSpacing(PANEL_SPACING)

        # 侧边栏
        self.sidebar = SpatialSidebar(
            performance_mode=self._performance_mode
        )
        self.sidebar.setFixedWidth(SIDEBAR_EXPANDED_WIDTH)
        body.addWidget(self.sidebar)

        # 中央区域（页面栈 + 悬浮输入）
        self.center = QWidget()
        self.center.setObjectName("spatialCenter")
        self.center.setAttribute(Qt.WA_TranslucentBackground, True)
        body.addWidget(self.center, 1)

        center_lay = QVBoxLayout(self.center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        self.stack = QStackedWidget()
        self.stack.setObjectName("spatialStack")
        self.stack.setAttribute(Qt.WA_TranslucentBackground, True)
        center_lay.addWidget(self.stack)

        self._build_pages()

        # 悬浮输入胶囊（保持原位置：中央区域底部居中）
        self.composer = SpatialComposer(
            self.center,
            performance_mode=self._performance_mode,
            blur_provider=self._blur_patch,
        )
        self.composer.show()

        # 右侧信息列
        self.right_col = QWidget()
        self.right_col.setObjectName("spatialRightColumn")
        self.right_col.setAttribute(Qt.WA_TranslucentBackground, True)
        self.right_col.setFixedWidth(RIGHT_COLUMN_WIDTH)

        rl = QVBoxLayout(self.right_col)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(PANEL_SPACING)

        self.quick_actions = QuickActionsCard(
            [
                ("✧", "新建对话", self.new_chat, True),
                ("▤", "打开工作区", self.open_workspace, True),
                ("✉", "对话记录", lambda: self.switch_page("chat"), True),
                ("⚙", "设置", self.open_settings, True),
            ],
            performance_mode=self._performance_mode,
        )
        rl.addWidget(self.quick_actions, 4)

        self.schedule_card = ScheduleCard(
            performance_mode=self._performance_mode
        )
        rl.addWidget(self.schedule_card, 4)

        self.model_card = ModelInfoCard(
            performance_mode=self._performance_mode
        )
        self.model_card.switch_requested.connect(self.open_settings)
        rl.addWidget(self.model_card, 3)

        body.addWidget(self.right_col)
        root.addLayout(body, 1)

        # 右侧信息面板也使用同一种隔离背景模糊
        for card in (self.quick_actions, self.schedule_card, self.model_card):
            try:
                card.set_blur_provider(self._blur_patch)
            except Exception:
                pass

        # 悬浮快捷栏 / 上下文菜单 / 边缘恢复入口
        self._build_overlays()

    # ── 悬浮层 ────────────────────────────────────────────

    def _build_overlays(self):
        self._left_visible = True
        self._right_visible = True

        entries = [
            ("home", "⌂", "首页"),
            ("chat", "✉", "聊天"),
            ("new", "＋", "新建对话"),
            ("model", "◐", "模型"),
            ("settings", "⚙", "设置"),
        ]
        self.quick_bar = QuickBar(
            self.center,
            entries=entries,
            performance_mode=self._performance_mode,
            blur_provider=self._blur_patch,
        )
        self.quick_bar.entry_clicked.connect(self._on_quick_entry)
        self.quick_bar.entry_context.connect(self._show_context_menu)
        self.quick_bar.set_active("home")
        self.quick_bar.show()

        self.context_menu = ContextPanel(
            self, performance_mode=self._performance_mode
        )
        self.context_menu.action_triggered.connect(self._on_context_action)

        self.left_handle = self._make_edge_handle("‹", "显示左侧导航")
        self.left_handle.clicked.connect(
            lambda: self._set_left_sidebar_visible(True)
        )
        self.right_handle = self._make_edge_handle("›", "显示右侧信息栏")
        self.right_handle.clicked.connect(
            lambda: self._set_right_column_visible(True)
        )
        self.left_handle.hide()
        self.right_handle.hide()

    def _make_edge_handle(self, text: str, tip: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(EDGE_HANDLE_W, EDGE_HANDLE_H)
        btn.setStyleSheet(
            "QToolButton {"
            " background: rgba(38,45,84,0.94);"
            " border: 1px solid rgba(160,182,255,0.32);"
            " border-radius: 8px;"
            f" color: {COLOR_TEXT_SECONDARY}; font-size: 13px;"
            "}"
            "QToolButton:hover {"
            " background: rgba(58,70,130,0.96);"
            " border-color: rgba(180,200,255,0.6);"
            f" color: {COLOR_TEXT_PRIMARY};"
            "}"
        )
        return btn

    # ── 隔离背景模糊（只取背景层）──────────────────────────

    def _blur_patch(self, widget):
        try:
            radius = PERFORMANCE_MODES.get(
                self._performance_mode, PERFORMANCE_MODES["medium"]
            ).get("blur_radius", 6)
            return self.space_bg.blur_patch(widget, radius)
        except Exception:
            return None

    def _refresh_panel_blur(self):
        for w in (self.sidebar, self.composer, self.quick_actions,
                  self.schedule_card, self.model_card,
                  getattr(self, "chat_panel", None)):
            if w is None:
                continue
            try:
                w.refresh_blur()
            except Exception:
                pass

    def _build_pages(self):
        # 首页
        self.home_page = HomePage(
            performance_mode=self._performance_mode,
            enable_3d=self._model_3d_enabled(),
            parent=self,
        )
        self.stack.addWidget(self.home_page)

        # 聊天页
        self.chat_page = self._build_chat_page()
        self.stack.addWidget(self.chat_page)

        # 未接入模块
        self.stack.addWidget(
            PlaceholderPage(
                "▣", "项目",
                "项目模块尚未接入当前版本。\n"
                "接入后将在此管理工程与任务。",
                parent=self,
            )
        )
        self.stack.addWidget(
            PlaceholderPage(
                "▤", "文件",
                "文件模块尚未接入当前版本。\n"
                "当前可通过「打开工作区」访问本地目录。",
                parent=self,
            )
        )
        self.stack.addWidget(
            PlaceholderPage(
                "◈", "日历",
                "日历模块尚未接入当前版本。\n"
                "接入后将在此显示日程与提醒。",
                parent=self,
            )
        )
        self.stack.addWidget(self._build_model_page())

        self._page_index = {
            "home": 0,
            "chat": 1,
            "project": 2,
            "file": 3,
            "calendar": 4,
            "model": 5,
        }

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        page.setAttribute(Qt.WA_TranslucentBackground, True)

        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 历史条
        head = GlassPanel(
            page, radius=16, tint=(28, 34, 64), fill_alpha=140,
            performance_mode=self._performance_mode, shadow_enabled=False,
        )
        head.setFixedHeight(44)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 6, 14, 6)
        hl.setSpacing(8)

        title = QLabel("对话记录")
        title.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 12px;"
            f" font-weight: 600; background: transparent;"
        )
        self.chat_count_label = QLabel("0 条")
        self.chat_count_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )

        self.new_chat_btn = QPushButton("＋ 新对话")
        self.new_chat_btn.setCursor(Qt.PointingHandCursor)
        self.new_chat_btn.setFixedHeight(26)
        self.new_chat_btn.setStyleSheet(
            "QPushButton { background: rgba(123,147,255,0.22);"
            " border: 1px solid rgba(160,185,255,0.35);"
            " border-radius: 13px; padding: 0 12px;"
            f" color: {COLOR_TEXT_PRIMARY}; font-size: 11px; }}"
            "QPushButton:hover { background: rgba(123,147,255,0.36); }"
        )
        self.new_chat_btn.clicked.connect(self.new_chat)

        self.history_btn = QPushButton("历史")
        self.history_btn.setCursor(Qt.PointingHandCursor)
        self.history_btn.setFixedHeight(26)
        self.history_btn.setStyleSheet(
            "QPushButton { background: transparent;"
            " border: 1px solid rgba(150,172,255,0.22);"
            " border-radius: 13px; padding: 0 12px;"
            f" color: {COLOR_TEXT_MUTED}; font-size: 11px; }}"
            "QPushButton:hover { color: #eef1ff;"
            " border-color: rgba(160,185,255,0.4); }"
        )
        self.history_btn.clicked.connect(self._show_history_menu)

        hl.addWidget(title)
        hl.addWidget(self.chat_count_label)
        hl.addStretch(1)
        hl.addWidget(self.history_btn)
        hl.addWidget(self.new_chat_btn)

        lay.addWidget(head)

        # 聊天主面板（位置/尺寸/布局不变，只是加统一面板材质 + 内容渐隐）
        self.chat_panel = GlassPanel(
            page,
            kind="main",
            tint=(36, 43, 80),
            performance_mode=self._performance_mode,
            blur_provider=self._blur_patch,
        )
        self.chat_panel.setObjectName("spatialChatPanel")
        panel_lay = QVBoxLayout(self.chat_panel)
        panel_lay.setContentsMargins(6, 6, 6, 6)
        panel_lay.setSpacing(0)

        # 消息滚动区
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("spatialChatScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.chat_container = QWidget()
        self.chat_container.setObjectName("spatialChatContainer")
        self.chat_layout = QVBoxLayout(self.chat_container)
        # 右侧留出悬浮快捷栏的宽度，底部留出输入胶囊
        self.chat_layout.setContentsMargins(20, 18, 74, 108)
        self.chat_layout.setSpacing(12)
        self.chat_layout.addStretch(1)

        self.chat_scroll.setWidget(self.chat_container)
        panel_lay.addWidget(self.chat_scroll, 1)
        lay.addWidget(self.chat_panel, 1)

        for w in (self.chat_scroll, self.chat_scroll.viewport(),
                  self.chat_container):
            w.setAttribute(Qt.WA_TranslucentBackground, True)
            w.setAutoFillBackground(False)

        # 内容区上下渐隐（只覆盖消息区域，不拦截鼠标）
        self.chat_fade = FadeOverlay(
            self.chat_scroll.viewport(),
            self.chat_scroll,
            color=(36, 43, 80),
        )
        self.chat_fade.set_performance_mode(self._performance_mode)

        return page

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        page.setAttribute(Qt.WA_TranslucentBackground, True)

        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 108)
        lay.setSpacing(10)
        lay.addStretch(1)

        card = GlassPanel(
            page, radius=PANEL_RADIUS, tint=(28, 34, 64), fill_alpha=152,
            performance_mode=self._performance_mode,
        )
        card.setMaximumWidth(420)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(26, 22, 26, 22)
        cl.setSpacing(8)

        t = QLabel("当前模型")
        t.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 15px;"
            f" font-weight: 700; background: transparent;"
        )

        self.model_page_label = QLabel("未配置")
        self.model_page_label.setWordWrap(True)
        self.model_page_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: 13px;"
            f" background: transparent;"
        )

        btn = QPushButton("打开设置")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(32)
        btn.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT_BLUE};"
            " border: none; border-radius: 16px;"
            " padding: 0 18px; color: #0f1330;"
            " font-size: 12px; font-weight: 700; }"
            "QPushButton:hover { background: #93a8ff; }"
        )
        btn.clicked.connect(self.open_settings)

        cl.addWidget(t)
        cl.addWidget(self.model_page_label)
        cl.addWidget(btn)

        lay.addWidget(card, alignment=Qt.AlignHCenter)
        lay.addStretch(2)
        return page

    # ── 信号 ──────────────────────────────────────────────

    def _connect_signals(self):
        self.topbar.page_selected.connect(self.switch_page)
        self.topbar.new_chat_requested.connect(self.new_chat)
        self.topbar.settings_clicked.connect(self.open_settings)
        self.topbar.sidebar_toggle.connect(self.sidebar.toggle_collapsed)

        self.sidebar.page_selected.connect(self.switch_page)
        self.sidebar.settings_clicked.connect(self.open_settings)
        self.sidebar.new_chat_requested.connect(self.new_chat)
        self.sidebar.collapsed_changed.connect(self._on_sidebar_collapsed)

        self.composer.message_sent.connect(self._on_message_sent)
        self.composer.voice_started.connect(self.start_voice_input)
        self.composer.attachment_requested.connect(self._pick_attachment)
        self.composer.emoji_requested.connect(self.composer.insert_text)

        self.model_card.switch_requested.connect(self.open_settings)

    def _on_sidebar_collapsed(self, collapsed: bool):
        self._relayout_composer()

    # ── 悬停输入框定位 ────────────────────────────────────

    def _relayout_composer(self):
        if not hasattr(self, "composer"):
            return
        cw = self.center.width()
        ch = self.center.height()
        w = min(760, max(360, int(cw * 0.78)))
        h = self.composer.height()
        x = int((cw - w) / 2)
        y = ch - h - 24
        self.composer.setGeometry(x, y, w, h)
        self.composer.raise_()

    # ── 页面切换 ──────────────────────────────────────────

    def switch_page(self, key: str):
        idx = self._page_index.get(key)
        if idx is None:
            return
        self.stack.setCurrentIndex(idx)
        self.sidebar.select_page(key)
        self.topbar.select_page(key)
        # 快捷栏高亮跟随页面（设置/新建不常驻高亮）
        if key in ("home", "chat", "model"):
            try:
                self.quick_bar.set_active(key)
            except Exception:
                pass
        self._position_overlays()
        self._relayout_composer()
        self._refresh_panel_blur()

    # ── 状态 ──────────────────────────────────────────────

    # ── 3D 运行时配置 ─────────────────────────────────────

    def _spatial_settings(self) -> dict:
        try:
            return dict(self.settings.settings.get("spatial_ui", {}))
        except Exception:
            return {}

    def _model_3d_enabled(self) -> bool:
        return bool(self._spatial_settings().get("model_runtime_enabled", True))

    def _save_spatial_setting(self, key: str, value):
        try:
            node = self.settings.settings.setdefault("spatial_ui", {})
            node[key] = value
            self.settings._save_settings()
        except Exception:
            pass

    def _refresh_status(self):
        provider = self.ai_client.get_provider()
        model = self.ai_client.get_model()
        connected = self.ai_client.is_configured()

        provider_name = "DeepSeek" if provider == "deepseek" else "OpenRouter"
        self.topbar.set_connection_state(connected)
        self.model_card.set_model_info(provider_name, model, connected)
        self.model_page_label.setText(
            f"{provider_name} · {model}\n"
            + ("连接状态：已配置 API Key" if connected
               else "连接状态：未配置 API Key")
        )

    # ── 左右侧栏独立显示 / 隐藏 ───────────────────────────

    def _set_left_sidebar_visible(self, visible: bool, animate: bool = True):
        self._left_visible = bool(visible)
        self._apply_sidebar_visibility(animate)
        _update_ui_state(left_sidebar_visible=self._left_visible)

    def _set_right_column_visible(self, visible: bool, animate: bool = True):
        self._right_visible = bool(visible)
        self._apply_sidebar_visibility(animate)
        _update_ui_state(right_column_visible=self._right_visible)

    def _apply_sidebar_visibility(self, animate: bool = True):
        self._animate_width(
            self.sidebar,
            SIDEBAR_EXPANDED_WIDTH if self._left_visible else 0,
            animate,
            hide_when_zero=not self._left_visible,
        )
        self._animate_width(
            self.right_col,
            RIGHT_COLUMN_WIDTH if self._right_visible else 0,
            animate,
            hide_when_zero=not self._right_visible,
        )
        self.left_handle.setVisible(not self._left_visible)
        self.right_handle.setVisible(not self._right_visible)
        QTimer.singleShot(0, self._position_overlays)
        QTimer.singleShot(SIDEBAR_ANIM_MS + 30, self._position_overlays)

    def _animate_width(self, widget, target: int, animate: bool,
                       hide_when_zero: bool):
        """宽度动画：隐藏后释放布局空间，不留空白占位。"""
        speed = self._perf.get("anim_speed", 1.0)
        if target > 0:
            widget.setVisible(True)
        widget.setMinimumWidth(0)

        if not animate or speed <= 0:
            widget.setMaximumWidth(target)
            if target == 0 and hide_when_zero:
                widget.setVisible(False)
            self._refresh_panel_blur()
            return

        anim = QPropertyAnimation(widget, b"maximumWidth", self)
        anim.setDuration(int(SIDEBAR_ANIM_MS / max(0.4, speed)))
        anim.setStartValue(widget.maximumWidth() if widget.maximumWidth() < 4096
                           else widget.width())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.InOutCubic)
        if target == 0 and hide_when_zero:
            anim.finished.connect(lambda: widget.setVisible(False))
        anim.finished.connect(self._refresh_panel_blur)
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        if not hasattr(self, "_width_anims"):
            self._width_anims = []
        self._width_anims.append(anim)

    def _restore_sidebar_visibility(self):
        state = _load_ui_state()
        self._left_visible = bool(state.get("left_sidebar_visible", True))
        self._right_visible = bool(state.get("right_column_visible", True))
        self._apply_sidebar_visibility(animate=False)

    # ── 悬浮层定位 ────────────────────────────────────────

    def _position_overlays(self):
        try:
            m = PANEL_MARGIN
            h = EDGE_HANDLE_H
            cy = max(60, (self.height() - h) // 2)

            if hasattr(self, "left_handle") and self.left_handle.isVisible():
                self.left_handle.setGeometry(m + 2, cy, EDGE_HANDLE_W, h)
                self.left_handle.raise_()
            if hasattr(self, "right_handle") and self.right_handle.isVisible():
                self.right_handle.setGeometry(
                    self.width() - m - EDGE_HANDLE_W - 2, cy,
                    EDGE_HANDLE_W, h)
                self.right_handle.raise_()

            if hasattr(self, "quick_bar") and self.quick_bar.isVisible():
                cw, ch = self.center.width(), self.center.height()
                bw, bh = self.quick_bar.width(), self.quick_bar.height()
                x = max(8, cw - bw - 12)
                y = max(20, int((ch - bh) / 2) - 30)
                self.quick_bar.move(x, y)
                self.quick_bar.raise_()
        except Exception:
            pass

    # ── 快捷栏交互 ────────────────────────────────────────

    def _on_quick_entry(self, key: str):
        if key == "new":
            self.new_chat()
        elif key == "settings":
            self.open_settings()
        elif key in ("home", "chat", "model"):
            self.switch_page(key)

    def _context_items_for(self, key: str):
        """每个入口对应真实动作，不虚构功能。"""
        toggle_left = (
            "‹", "隐藏左侧导航" if self._left_visible else "显示左侧导航"
        )
        toggle_right = (
            "›", "隐藏右侧信息栏" if self._right_visible else "显示右侧信息栏"
        )
        if key == "home":
            return [
                ("reset_view", "◎", "重置模型视角", True),
                ("reload_model", "⟳", "重新加载模型", True),
                ("toggle_left", toggle_left[0], toggle_left[1], True),
                ("toggle_right", toggle_right[0], toggle_right[1], True),
            ]
        if key == "chat":
            return [
                ("new_chat", "＋", "新建对话", True),
                ("history", "≡", "历史会话", True),
                ("scroll_bottom", "↓", "回到最新消息", True),
                ("toggle_left", toggle_left[0], toggle_left[1], True),
            ]
        if key == "new":
            return [("new_chat", "＋", "新建对话", True)]
        if key == "model":
            return [
                ("model_page", "◐", "模型页", True),
                ("reset_view", "◎", "重置视角", True),
                ("reload_model", "⟳", "重新加载模型", True),
                ("toggle_3d", "◌", "启用 / 关闭 3D 运行时", True),
            ]
        if key == "settings":
            return [
                ("open_settings", "⚙", "打开设置", True),
                ("perf_low", "▁", "性能：Low", True),
                ("perf_medium", "▄", "性能：Balanced", True),
                ("perf_high", "█", "性能：High", True),
                ("toggle_right", toggle_right[0], toggle_right[1], True),
            ]
        return []

    def _show_context_menu(self, key: str, global_pos: QPoint):
        items = self._context_items_for(key)
        if not items:
            return
        titles = {
            "home": "首页", "chat": "聊天", "new": "新建",
            "model": "模型", "settings": "设置",
        }
        self.context_menu.set_items(titles.get(key, key), items)
        anchor = self.quick_bar.entry_rect_global(key)
        if anchor.isNull():
            self.context_menu.open_near(QRect(global_pos, self.quick_bar.size()))
        else:
            self.context_menu.open_near(anchor)

    def _on_context_action(self, key: str):
        if key == "new_chat":
            self.new_chat()
        elif key == "history":
            self._show_history_menu()
        elif key == "scroll_bottom":
            self._scroll_to_bottom()
        elif key == "model_page":
            self.switch_page("model")
        elif key == "reset_view":
            self.home_page.reset_model_view()
        elif key == "reload_model":
            self.home_page.reload_model()
        elif key == "toggle_3d":
            st = self.home_page.stage
            st.enable_runtime(st.runtime_state() != "ready")
        elif key == "open_settings":
            self.open_settings()
        elif key == "perf_low":
            self.set_performance_mode("low")
        elif key == "perf_medium":
            self.set_performance_mode("medium")
        elif key == "perf_high":
            self.set_performance_mode("high")
        elif key == "toggle_left":
            self._set_left_sidebar_visible(not self._left_visible)
        elif key == "toggle_right":
            self._set_right_column_visible(not self._right_visible)

    # ── 状态恢复 ──────────────────────────────────────────

    def _restore_state(self):
        self._refresh_history()
        self._restore_current_conversation()
        try:
            self._restore_sidebar_visibility()
        except Exception:
            pass

    def _refresh_history(self):
        try:
            conversations = self.ai_client.list_conversations()
        except Exception:
            conversations = []
        try:
            self.chat_count_label.setText(f"{len(conversations)} 条")
        except Exception:
            pass

    def _show_history_menu(self):
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        try:
            conversations = self.ai_client.list_conversations()
        except Exception:
            conversations = []
        if not conversations:
            act = menu.addAction("暂无历史会话")
            act.setEnabled(False)
        else:
            for conv in conversations[:20]:
                title = conv.get("title") or "新对话"
                sid = conv.get("id") or ""
                act = menu.addAction(title)
                act.triggered.connect(
                    lambda _c=False, s=sid: self._open_conversation(s)
                )
        menu.exec(self.history_btn.mapToGlobal(
            self.history_btn.rect().bottomLeft()
        ))

    def _open_conversation(self, session_id: str):
        if not session_id:
            return
        try:
            if self.ai_client.switch_conversation(session_id):
                self._restore_current_conversation()
                self.switch_page("chat")
            else:
                QMessageBox.warning(
                    self, "无法打开对话", "这条会话已不存在。")
        except Exception as exc:
            QMessageBox.critical(self, "切换失败", str(exc))

    def _restore_current_conversation(self):
        try:
            session = self.ai_client.get_current_session()
        except Exception:
            session = None
        if session:
            self._display_messages(session.get("messages", []))
        else:
            self._clear_chat()

    # ── 消息 ──────────────────────────────────────────────

    def _display_messages(self, messages):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for msg in messages:
            role = msg.get("role", "assistant")
            content = msg.get("content", "") or ""
            if not content:
                continue
            self._insert_bubble(role, content)
        self._scroll_to_bottom()

    def _insert_bubble(self, role: str, text: str):
        bubble = MessageBubble(role, text)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        return bubble

    def add_message(self, role: str, text: str):
        self._insert_bubble(role, text)
        if role == "user":
            QTimer.singleShot(30, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        bar = self.chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _clear_chat(self):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @Slot(str)
    def _on_message_sent(self, text: str):
        if self._ai_busy:
            return
        if self.stack.currentIndex() != self._page_index["chat"]:
            self.switch_page("chat")
        self.add_message("user", text)
        self.home_page.set_status_text("Arona 正在思考……")
        self._ai_busy = True

        thread = QThread(self)
        worker = _AIWorker(self.ai_client, text)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_ai_finished)
        worker.error.connect(self._on_ai_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._ai_thread = thread
        self._ai_worker = worker
        thread.start()

    @Slot(str)
    def _on_ai_finished(self, text: str):
        self._ai_busy = False
        self.add_message("assistant", text)
        self.home_page.set_status_text("Arona 正在等待你的消息～")
        self._scroll_to_bottom()
        self._refresh_history()
        self._refresh_status()

    @Slot(str)
    def _on_ai_error(self, err: str):
        self._ai_busy = False
        self.home_page.set_status_text("请求失败，请检查配置")
        self.add_message("system", f"请求失败：{err}")
        QMessageBox.warning(
            self, "AI 请求失败",
            "无法连接 AI 服务。\n\n请检查设置中的 API Key 与服务商配置。",
        )

    # ── 动作 ──────────────────────────────────────────────

    def new_chat(self):
        # D3 修复：正式 AIClient 无 new_session()（仅预览 mock 有）。
        # 改调 create_conversation（ai_client.py L2571），真正新建会话。
        try:
            self.ai_client.create_conversation(title="新对话")
        except Exception:
            try:
                self.ai_client.clear_history()
            except Exception:
                pass
        self._clear_chat()
        self.switch_page("chat")
        self.home_page.set_status_text("新的对话开始啦～")
        self._refresh_history()

    def open_workspace(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        workspace = Path(__file__).parent.parent / "workspace"
        if workspace.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(workspace)))
        else:
            QMessageBox.information(
                self, "工作区", f"工作区目录不存在：\n{workspace}")

    def _pick_attachment(self):
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "选择附件", "",
            "所有文件 (*.*);;图片 (*.png *.jpg *.jpeg *.webp);;"
            "文档 (*.pdf *.txt *.md *.json)",
        )
        if path:
            self.composer.insert_text(f"[附件] {Path(path).name}")
            self.add_message(
                "system", f"已选择附件：{Path(path).name}（发送接口沿用原版）"
            )

    def start_voice_input(self):
        QMessageBox.information(
            self, "语音输入",
            "语音识别模块（core.speech）已就绪，"
            "空间界面的语音按钮将在下一阶段接入。",
        )

    # ── 设置 ──────────────────────────────────────────────

    def open_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Arona 设置")
        dialog.setMinimumWidth(460)

        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)

        title = QLabel("设置")
        title.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-size: 18px;"
            f" font-weight: 700; background: transparent;"
        )
        lay.addWidget(title)

        ai_group = QGroupBox("AI 服务")
        ai_form = QFormLayout(ai_group)
        ai_form.setSpacing(10)

        provider_combo = QComboBox()
        provider_combo.addItem("DeepSeek", "deepseek")
        provider_combo.addItem("OpenRouter", "openrouter")
        cur = self.ai_client.get_provider()
        idx = provider_combo.findData(cur)
        if idx >= 0:
            provider_combo.setCurrentIndex(idx)
        ai_form.addRow("服务商", provider_combo)

        model_edit = QLineEdit(self.ai_client.get_model())
        model_edit.setPlaceholderText("例如 deepseek-chat")
        ai_form.addRow("模型", model_edit)

        lay.addWidget(ai_group)

        perf_group = QGroupBox("性能模式")
        perf_form = QFormLayout(perf_group)
        perf_combo = QComboBox()
        for mode, label in (
            ("high", "高（粒子/光晕）"),
            ("medium", "中（默认）"),
            ("low", "低（最低开销）"),
        ):
            perf_combo.addItem(label, mode)
        pidx = perf_combo.findData(self._performance_mode)
        if pidx >= 0:
            perf_combo.setCurrentIndex(pidx)
        perf_form.addRow("模式", perf_combo)
        lay.addWidget(perf_group)

        # ── 3D 模型运行时 ───────────────────────
        model3d_group = QGroupBox("Arona 3D 模型")
        model3d_form = QFormLayout(model3d_group)

        from PySide6.QtWidgets import QCheckBox

        self._model3d_check = QCheckBox("启用 3D 运行时")
        self._model3d_check.setChecked(self._model_3d_enabled())
        model3d_form.addRow(self._model3d_check)

        runtime_state = self.home_page.stage.runtime_state()
        state_label = QLabel(
            {
                "ready": "状态：已加载",
                "loading": "状态：加载中…",
                "failed": "状态：加载失败（占位模式）",
                "disabled": "状态：已关闭",
                "off": "状态：未启动",
            }.get(runtime_state, f"状态：{runtime_state}")
        )
        state_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )
        model3d_form.addRow(state_label)

        model3d_btns = QHBoxLayout()
        reload_btn = QPushButton("重新加载")
        reload_btn.clicked.connect(self.home_page.reload_model)
        reset_btn = QPushButton("重置视角")
        reset_btn.clicked.connect(self.home_page.reset_model_view)
        model3d_btns.addWidget(reload_btn)
        model3d_btns.addWidget(reset_btn)
        model3d_btns.addStretch(1)
        model3d_form.addRow(model3d_btns)

        lay.addWidget(model3d_group)

        note = QLabel(
            "提示：API Key 由 core.settings 通过 Windows 凭据管理器保存，"
            "此处仅切换服务商与模型。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f" background: transparent;"
        )
        lay.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        save = QPushButton("保存")
        save.setObjectName("primaryButton")

        def _save():
            provider = provider_combo.currentData()
            model = model_edit.text().strip()
            if provider:
                self.ai_client.set_provider(provider)
            if model:
                self.ai_client.set_model(model)
            mode = perf_combo.currentData()
            if mode in PERFORMANCE_MODES:
                self.set_performance_mode(mode)

            enabled_3d = bool(self._model3d_check.isChecked())
            self._save_spatial_setting("model_runtime_enabled", enabled_3d)
            self.home_page.set_3d_enabled(enabled_3d)

            self._refresh_status()
            dialog.accept()

        save.clicked.connect(_save)

        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

        dialog.exec()

    # ── 性能模式 ──────────────────────────────────────────

    def set_performance_mode(self, mode: str):
        if mode not in PERFORMANCE_MODES:
            return
        self._performance_mode = mode
        self._perf = PERFORMANCE_MODES[mode]

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_app_qss(performance_mode=mode))

        targets = [
            self.space_bg, self.sidebar, self.topbar,
            self.composer, self.home_page,
        ]
        for name in ("quick_actions", "schedule_card", "model_card",
                     "chat_panel", "quick_bar"):
            w = getattr(self, name, None)
            if w is not None:
                targets.append(w)
        for w in targets:
            if hasattr(w, "set_performance_mode"):
                try:
                    w.set_performance_mode(mode)
                except Exception:
                    pass
        if hasattr(self, "chat_fade"):
            try:
                self.chat_fade.set_performance_mode(mode)
            except Exception:
                pass
        self._refresh_panel_blur()
        self.performance_mode_changed.emit(mode)

    # ── 窗口事件 ──────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self.space_bg.setGeometry(self.rect())
        self._relayout_composer()
        QTimer.singleShot(0, self._position_overlays)
        QTimer.singleShot(120, self._refresh_panel_blur)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "space_bg"):
            self.space_bg.setGeometry(self.rect())
        self._relayout_composer()
        self._position_overlays()
        # 背景缓存刚重建，面板模糊贴片需要重新取（延迟到布局稳定后）
        QTimer.singleShot(60, self._refresh_panel_blur)

    def closeEvent(self, event):
        try:
            _update_ui_state(
                left_sidebar_visible=self._left_visible,
                right_column_visible=self._right_visible,
            )
        except Exception:
            pass
        try:
            self.home_page.status_card.stop_monitoring()
        except Exception:
            pass
        # 释放 3D 运行时（GL 资源必须在窗口销毁前清理）
        try:
            self.home_page.stage.enable_runtime(False)
        except Exception:
            pass
        try:
            self.window_manager.save_all_geometry()
        except Exception:
            pass
        event.accept()


# ──────────────────────────────────────────────────────────

class _AIWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, ai_client, message: str):
        super().__init__()
        self.ai_client = ai_client
        self.message = message

    @Slot()
    def run(self):
        try:
            # D4 修复：正式 AIClient 无 chat() 方法（该方法仅存在于预览
            # mock PreviewAIClient）。改调 send_message(message,
            # attachments=None, save_to_session=True)，与 L1 AIWorker
            # （ui.py AIWorker.run）语义一致。
            result = self.ai_client.send_message(self.message)
            if result is None:
                result = ""
            self.finished.emit(str(result))
        except Exception as exc:
            self.error.emit(str(exc))


# ──────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="AronaClient L2 Spatial UI")
    parser.add_argument(
        "--performance", "-p",
        choices=["high", "medium", "low"],
        default="medium",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("AronaClient")
    app.setOrganizationName("Arona")

    # T1/T4 注入：正式入口使用共享 AIClient / SettingsManager
    # （services.app_context.AppContext，T4 落地；未落地时回退到
    # 窗口自构造，行为与现状一致，L1 零影响）。
    try:
        from services.app_context import AppContext
        _ctx = AppContext.get()
        window = SpatialMainWindow(
            performance_mode=args.performance,
            ai_client=_ctx.ai_client,
            settings=_ctx.settings,
        )
    except ImportError:
        window = SpatialMainWindow(performance_mode=args.performance)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
