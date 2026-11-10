"""
spatial_ui.theme — 应用级全局主题（QSS）。

关键点：必须通过 QApplication.setStyleSheet() 应用，
才会传播到 QDialog / QMessageBox / QMenu 等独立顶层窗口。
仅对主窗口 setStyleSheet 会导致设置对话框、消息框仍为系统原生白皮。

覆盖：QWidget / QLabel / QPushButton / QToolButton / QLineEdit /
QTextEdit / QComboBox / QScrollArea / QScrollBar / QListWidget /
QGroupBox / QDialog / QMessageBox / QMenu / QSlider / QCheckBox
"""

import warnings
warnings.filterwarnings("ignore")

from spatial_ui.styles import (
    COLOR_SPACE_EDGE,
    COLOR_ACCENT_BLUE,
    COLOR_ACCENT_PURPLE,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_INSET,
    COLOR_INSET_BORDER,
    COLOR_HOVER_BG,
    FONT_FAMILY,
    FONT_MONO,
)

# 中性深色：所有原生控件的兜底底色（关键：杜绝白底）
_BASE_BG = "#12172e"
_INPUT_BG = "rgba(16, 21, 42, 0.92)"
_BORDER = "rgba(140, 160, 235, 0.20)"
_BORDER_FOCUS = "rgba(123, 147, 255, 0.65)"


def build_app_qss(*, performance_mode: str = "medium") -> str:
    """生成应用级全局 QSS。"""
    return f"""
/* ===================== 基础 ===================== */
* {{
    font-family: {FONT_FAMILY};
    outline: none;
}}

QWidget {{
    color: {COLOR_TEXT_PRIMARY};
    font-size: 14px;
    selection-background-color: {COLOR_ACCENT_BLUE};
    selection-color: #0b0f20;
}}

QMainWindow, QDialog {{
    background-color: {_BASE_BG};
}}

/* 避免 QLabel 继承到不想要的边框 */
QLabel {{
    background: transparent;
    border: none;
}}

/* ===================== 按钮 ===================== */
QPushButton {{
    background: {COLOR_INSET};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 8px 16px;
    color: {COLOR_TEXT_PRIMARY};
    font-size: 13px;
}}
QPushButton:hover {{
    background: {COLOR_HOVER_BG};
    border-color: {_BORDER_FOCUS};
}}
QPushButton:pressed {{
    background: rgba(123, 147, 255, 0.28);
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_MUTED};
    border-color: rgba(140, 160, 235, 0.10);
}}

QPushButton#primaryButton {{
    background: {COLOR_ACCENT_BLUE};
    border: none;
    color: #0d1024;
    font-weight: 700;
}}
QPushButton#primaryButton:hover {{
    background: #93a8ff;
}}

QToolButton {{
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 6px 8px;
    color: {COLOR_TEXT_SECONDARY};
}}
QToolButton:hover {{
    background: {COLOR_HOVER_BG};
    color: {COLOR_TEXT_PRIMARY};
}}
QToolButton:pressed {{
    background: rgba(123, 147, 255, 0.22);
}}
QToolButton::menu-indicator {{
    image: none;
}}

/* ===================== 输入类 ===================== */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background: {_INPUT_BG};
    border: 1px solid {_BORDER};
    border-radius: 9px;
    padding: 7px 10px;
    color: {COLOR_TEXT_PRIMARY};
    selection-background-color: {COLOR_ACCENT_BLUE};
    selection-color: #0b0f20;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {_BORDER_FOCUS};
}}
QTextEdit {{
    padding: 6px;
}}

QComboBox {{
    background: {_INPUT_BG};
    border: 1px solid {_BORDER};
    border-radius: 9px;
    padding: 7px 12px;
    color: {COLOR_TEXT_PRIMARY};
}}
QComboBox:hover {{
    border-color: {_BORDER_FOCUS};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {COLOR_TEXT_MUTED};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: #161c36;
    border: 1px solid {_BORDER};
    border-radius: 8px;
    color: {COLOR_TEXT_PRIMARY};
    selection-background-color: {COLOR_ACCENT_BLUE};
    selection-color: #0b0f20;
    outline: none;
    padding: 4px;
}}

/* ===================== 分组框 ===================== */
QGroupBox {{
    background: rgba(26, 32, 60, 0.55);
    border: 1px solid {_BORDER};
    border-radius: 12px;
    margin-top: 16px;
    padding: 14px 12px 12px 12px;
    color: {COLOR_TEXT_SECONDARY};
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    color: {COLOR_ACCENT_BLUE};
    font-size: 12px;
}}

/* ===================== 列表 ===================== */
QListWidget, QListView {{
    background: transparent;
    border: none;
    color: {COLOR_TEXT_SECONDARY};
    outline: none;
}}
QListWidget::item {{
    border-radius: 9px;
    padding: 7px 8px;
    margin: 2px 0;
}}
QListWidget::item:hover {{
    background: {COLOR_HOVER_BG};
}}
QListWidget::item:selected {{
    background: rgba(123, 147, 255, 0.26);
    color: {COLOR_TEXT_PRIMARY};
}}

/* ===================== 滚动区 ===================== */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 4px 2px 4px 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(140, 160, 235, 0.28);
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(140, 160, 235, 0.5);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: rgba(140, 160, 235, 0.28);
    border-radius: 4px;
    min-width: 28px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ===================== 滑块 / 勾选 ===================== */
QSlider::groove:horizontal {{
    height: 5px;
    background: rgba(140, 160, 235, 0.18);
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {COLOR_ACCENT_BLUE};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    margin: -5px 0;
    background: #dfe6ff;
    border-radius: 7px;
}}

QCheckBox {{
    color: {COLOR_TEXT_SECONDARY};
    spacing: 7px;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid {_BORDER};
    background: {COLOR_INSET};
}}
QCheckBox::indicator:checked {{
    background: {COLOR_ACCENT_BLUE};
    border-color: {COLOR_ACCENT_BLUE};
}}

/* ===================== 菜单 ===================== */
QMenu {{
    background-color: #171d38;
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 6px;
    color: {COLOR_TEXT_PRIMARY};
}}
QMenu::item {{
    padding: 7px 22px 7px 12px;
    border-radius: 7px;
}}
QMenu::item:selected {{
    background: rgba(123, 147, 255, 0.26);
}}
QMenu::separator {{
    height: 1px;
    background: rgba(140, 160, 235, 0.16);
    margin: 5px 8px;
}}

/* ===================== 提示框 / 消息框 ===================== */
QMessageBox {{
    background-color: #12172e;
}}
QMessageBox QLabel {{
    color: {COLOR_TEXT_PRIMARY};
    font-size: 13px;
}}
QMessageBox QPushButton {{
    min-width: 78px;
    padding: 7px 14px;
}}

/* ===================== 工具提示 ===================== */
QToolTip {{
    background-color: #1b2142;
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}}

/* ===================== 选项卡 ===================== */
QTabWidget::pane {{
    border: 1px solid {_BORDER};
    border-radius: 10px;
    background: rgba(20, 26, 48, 0.6);
}}
QTabBar::tab {{
    background: transparent;
    color: {COLOR_TEXT_MUTED};
    padding: 7px 14px;
    border-radius: 8px;
    margin-right: 4px;
}}
QTabBar::tab:selected {{
    background: rgba(123, 147, 255, 0.22);
    color: {COLOR_TEXT_PRIMARY};
}}

/* ===================== 进度条 ===================== */
QProgressBar {{
    background: rgba(140, 160, 235, 0.15);
    border: none;
    border-radius: 3px;
    height: 5px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {COLOR_ACCENT_BLUE};
    border-radius: 3px;
}}
"""
