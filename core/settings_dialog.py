from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.ai_client import AIClient
from core.settings import SettingsManager


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Arona 设置")
        self.setMinimumWidth(520)

        self.settings = SettingsManager()

        self.build_ui()
        self.load_settings()

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Arona 设置")
        title.setObjectName("settingsTitle")
        root.addWidget(title)

        subtitle = QLabel(
            "配置 AI 服务、模型以及 Arona 的基础行为。"
        )
        subtitle.setObjectName("settingsSubtitle")
        root.addWidget(subtitle)

        ai_group = QGroupBox("AI 服务")
        ai_layout = QFormLayout(ai_group)
        ai_layout.setSpacing(12)

        self.provider_combo = QComboBox()
        self.provider_combo.addItem("DeepSeek", "deepseek")
        self.provider_combo.addItem("OpenRouter", "openrouter")

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText(
            "例如：deepseek-chat"
        )

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Password
        )
        self.api_key_edit.setPlaceholderText(
            "输入 API Key"
        )

        ai_layout.addRow("服务商", self.provider_combo)
        ai_layout.addRow("模型", self.model_edit)
        ai_layout.addRow("API Key", self.api_key_edit)

        root.addWidget(ai_group)

        voice_group = QGroupBox("语音")
        voice_layout = QVBoxLayout(voice_group)

        self.voice_enabled = QCheckBox(
            "启用语音输入"
        )

        self.microphone_label = QLabel(
            "麦克风：检测中……"
        )
        self.microphone_label.setObjectName(
            "microphoneLabel"
        )

        voice_layout.addWidget(
            self.voice_enabled
        )
        voice_layout.addWidget(
            self.microphone_label
        )

        root.addWidget(voice_group)

        appearance_group = QGroupBox("外观")
        appearance_layout = QFormLayout(
            appearance_group
        )

        self.theme_combo = QComboBox()
        self.theme_combo.addItem(
            "深色",
            "dark",
        )

        appearance_layout.addRow(
            "主题",
            self.theme_combo,
        )

        root.addWidget(appearance_group)

        status_layout = QHBoxLayout()

        self.status_label = QLabel(
            "● 未测试连接"
        )
        self.status_label.setObjectName(
            "statusLabel"
        )

        status_layout.addWidget(
            self.status_label
        )
        status_layout.addStretch()

        self.test_button = QPushButton(
            "测试连接"
        )
        self.test_button.setObjectName(
            "testConnectionButton"
        )
        self.test_button.clicked.connect(
            self.test_connection
        )

        status_layout.addWidget(
            self.test_button
        )

        root.addLayout(status_layout)

        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.cancel_button = QPushButton(
            "取消"
        )
        self.cancel_button.clicked.connect(
            self.reject
        )

        self.save_button = QPushButton(
            "保存设置"
        )
        self.save_button.setObjectName(
            "primaryButton"
        )
        self.save_button.clicked.connect(
            self.save_settings
        )

        button_layout.addWidget(
            self.cancel_button
        )
        button_layout.addWidget(
            self.save_button
        )

        root.addLayout(button_layout)

        self.provider_combo.currentIndexChanged.connect(
            self.provider_changed
        )

        self.setStyleSheet(
            """
            QDialog {
                background: #111318;
                color: #f4f4f5;
            }

            QLabel {
                color: #e5e7eb;
            }

            #settingsTitle {
                font-size: 24px;
                font-weight: 700;
                color: #f4f4f5;
            }

            #settingsSubtitle {
                color: #b4bac6;
                margin-bottom: 8px;
            }

            QGroupBox {
                border: 1px solid #292d36;
                border-radius: 12px;
                margin-top: 10px;
                padding: 16px;
                font-weight: 600;
                color: #e5e7eb;
            }

            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #e5e7eb;
            }

            QComboBox,
            QLineEdit {
                background: #191c23;
                border: 1px solid #30343e;
                border-radius: 8px;
                padding: 9px 10px;
                color: #f4f4f5;
                min-height: 18px;
            }

            QComboBox:focus,
            QLineEdit:focus {
                border: 1px solid #7287ff;
            }

            QCheckBox {
                spacing: 8px;
                color: #e5e7eb;
            }

            QPushButton {
                background: #20242d;
                border: 1px solid #30343e;
                border-radius: 8px;
                padding: 9px 16px;
                color: #f4f4f5;
            }

            QPushButton:hover {
                background: #292e38;
            }

            #testConnectionButton {
                color: #e5e7eb;
                background: #20242d;
                border: 1px solid #30343e;
            }

            #testConnectionButton:hover {
                background: #292e38;
            }

            #testConnectionButton:disabled {
                color: #9ca3af;
            }

            #primaryButton {
                background: #5865f2;
                border: none;
                font-weight: 600;
                color: #ffffff;
            }

            #primaryButton:hover {
                background: #6975ff;
            }

            #statusLabel {
                color: #c7ccd6;
                font-weight: 500;
            }

            #microphoneLabel {
                color: #b4bac6;
            }
            """
        )

    def load_settings(self):
        provider = self.settings.get_provider()

        index = self.provider_combo.findData(
            provider
        )

        if index >= 0:
            self.provider_combo.setCurrentIndex(
                index
            )

        self.model_edit.setText(
            self.settings.get_model(provider)
        )

        api_key = self.settings.get_api_key(
            provider
        )

        if api_key:
            self.api_key_edit.setText(api_key)
            self.status_label.setText(
                "● API Key 已配置"
            )
        else:
            self.status_label.setText(
                "● 未配置 API Key"
            )

        self.voice_enabled.setChecked(
            self.settings.get_voice_enabled()
        )

        theme = self.settings.get_theme()

        theme_index = self.theme_combo.findData(
            theme
        )

        if theme_index >= 0:
            self.theme_combo.setCurrentIndex(
                theme_index
            )

        self.update_microphone_status()

    def provider_changed(self):
        provider = self.provider_combo.currentData()

        if not provider:
            return

        self.model_edit.setText(
            self.settings.get_model(provider)
        )

        api_key = self.settings.get_api_key(
            provider
        )

        self.api_key_edit.setText(api_key)

        if api_key:
            self.status_label.setText(
                "● API Key 已配置"
            )
        else:
            self.status_label.setText(
                "● 未配置 API Key"
            )

    def update_microphone_status(self):
        try:
            from core.speech import SpeechRecognizer

            recognizer = SpeechRecognizer()

            name = recognizer.get_device_name()

            if name:
                self.microphone_label.setText(
                    f"麦克风：{name}"
                )
            else:
                self.microphone_label.setText(
                    "麦克风：未检测到"
                )

        except Exception:
            self.microphone_label.setText(
                "麦克风：无法检测"
            )

    def test_connection(self):
        provider = self.provider_combo.currentData()
        model = self.model_edit.text().strip()
        api_key = self.api_key_edit.text().strip()

        if not api_key:
            QMessageBox.warning(
                self,
                "缺少 API Key",
                "请先输入 API Key。",
            )
            return

        if not model:
            QMessageBox.warning(
                self,
                "缺少模型",
                "请先输入模型名称。",
            )
            return

        self.test_button.setEnabled(False)
        self.test_button.setText("测试中……")
        self.status_label.setText(
            "● 正在连接 AI 服务……"
        )

        try:
            temp_settings = SettingsManager()

            temp_settings.save(
                provider,
                model,
                api_key,
            )

            client = AIClient()

            if not client.is_configured():
                raise RuntimeError(
                    "AI 客户端没有正确读取 API Key。"
                )

            response = client.send_message(
                "请只回复：连接正常"
            )

            if response:
                self.status_label.setText(
                    "● 连接成功"
                )

                QMessageBox.information(
                    self,
                    "连接成功",
                    "Arona 已成功连接到 AI 服务。",
                )
            else:
                raise RuntimeError(
                    "AI 服务没有返回内容。"
                )

        except Exception as e:
            self.status_label.setText(
                "● 连接失败"
            )

            QMessageBox.critical(
                self,
                "连接失败",
                f"无法连接 AI 服务。\n\n{e}",
            )

        finally:
            self.test_button.setEnabled(True)
            self.test_button.setText(
                "测试连接"
            )

    def save_settings(self):
        provider = self.provider_combo.currentData()
        model = self.model_edit.text().strip()
        api_key = self.api_key_edit.text().strip()

        if not provider:
            QMessageBox.warning(
                self,
                "设置错误",
                "请选择 AI 服务商。",
            )
            return

        if not model:
            QMessageBox.warning(
                self,
                "设置错误",
                "模型名称不能为空。",
            )
            return

        try:
            self.settings.save(
                provider,
                model,
                api_key,
            )

            self.settings.set_voice_enabled(
                self.voice_enabled.isChecked()
            )

            self.settings.set_theme(
                self.theme_combo.currentData()
            )

            self.accept()

        except Exception as e:
            QMessageBox.critical(
                self,
                "保存失败",
                f"设置保存失败：\n\n{e}",
            )