import json
from pathlib import Path

import keyring

from config import DEFAULT_PROVIDER


class SettingsManager:
    APP_NAME = "AronaClient"

    SETTINGS_DIR = (
        Path.home()
        / "AppData"
        / "Roaming"
        / APP_NAME
    )

    SETTINGS_FILE = (
        SETTINGS_DIR
        / "settings.json"
    )

    KEYRING_SERVICE = "AronaClient"

    DEFAULT_MODELS = {
        "deepseek": "deepseek-chat",
        "openrouter": "deepseek/deepseek-chat",
    }

    def __init__(self):
        self.SETTINGS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.settings = (
            self._load_settings()
        )

    def _default_settings(self):
        return {
            "provider": DEFAULT_PROVIDER,
            "models": {
                "deepseek": self.DEFAULT_MODELS[
                    "deepseek"
                ],
                "openrouter": self.DEFAULT_MODELS[
                    "openrouter"
                ],
            },
            "appearance": {
                "theme": "dark",
            },
            "voice": {
                "enabled": True,
            },
            # 桌面视觉（截图 / 屏幕信息 / 前台窗口信息）
            #
            # 默认【关闭】。这是安全默认值：
            # 读取桌面像素与前台窗口信息属于敏感能力，
            # 必须由用户显式开启，而不是默认可用。
            "desktop_vision": {
                "enabled": False,
            },
            # 桌面自主感知（持续观察 + AI 分析 + 主动提醒）
            #
            # 默认【关闭】。依赖桌面视觉授权。
            "desktop_awareness": {
                "enabled": False,
                "interval_seconds": 30,
            },
        }

    def _load_settings(self):
        defaults = (
            self._default_settings()
        )

        if not self.SETTINGS_FILE.exists():
            self._save_settings(
                defaults
            )
            return defaults

        try:
            with open(
                self.SETTINGS_FILE,
                "r",
                encoding="utf-8",
            ) as f:
                loaded = json.load(f)

            if not isinstance(
                loaded,
                dict,
            ):
                return defaults

            if "provider" in loaded:
                defaults["provider"] = (
                    loaded["provider"]
                )

            if isinstance(
                loaded.get("models"),
                dict,
            ):
                defaults["models"].update(
                    loaded["models"]
                )

            if isinstance(
                loaded.get("appearance"),
                dict,
            ):
                defaults[
                    "appearance"
                ].update(
                    loaded["appearance"]
                )

            if isinstance(
                loaded.get("voice"),
                dict,
            ):
                defaults["voice"].update(
                    loaded["voice"]
                )

            # 桌面视觉开关：fail-closed 解析。
            #
            # 只接受【真正的 bool】True 才开启；其余一切情况
            # （缺字段、类型错误、损坏、字符串 "true"、数字 1 …）
            # 一律保持默认的 False。
            #
            # 这样即使 settings.json 被手工改成
            # {"desktop_vision": {"enabled": "yes"}}，也不会放开权限。
            _dv = loaded.get("desktop_vision")

            if isinstance(_dv, dict):

                _dv_enabled = _dv.get("enabled")

                if _dv_enabled is True:

                    defaults["desktop_vision"]["enabled"] = True

            # 桌面自主感知：fail-closed 解析。
            # 只接受【真正的 dict】且包含正确类型的字段。
            _da = loaded.get("desktop_awareness")

            if isinstance(_da, dict):
                _da_enabled = _da.get("enabled")
                if _da_enabled is True:
                    defaults["desktop_awareness"]["enabled"] = True
                _da_interval = _da.get("interval_seconds")
                if isinstance(_da_interval, int) and 5 <= _da_interval <= 600:
                    defaults["desktop_awareness"]["interval_seconds"] = _da_interval

            return defaults

        except Exception:
            return defaults

    def _save_settings(
        self,
        data=None,
    ):
        if data is None:
            data = self.settings

        self.SETTINGS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_file = (
            self.SETTINGS_FILE.with_suffix(
                ".tmp"
            )
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        temp_file.replace(
            self.SETTINGS_FILE
        )

    def get_provider(self):
        return self.settings.get(
            "provider",
            DEFAULT_PROVIDER,
        )

    def set_provider(
        self,
        provider,
    ):
        provider = str(
            provider
        ).strip().lower()

        if provider not in (
            "deepseek",
            "openrouter",
        ):
            raise ValueError(
                f"不支持的 AI 服务商：{provider}"
            )

        self.settings[
            "provider"
        ] = provider

        self._save_settings()

    def get_model(
        self,
        provider=None,
    ):
        if provider is None:
            provider = (
                self.get_provider()
            )

        provider = str(
            provider
        ).strip().lower()

        return self.settings.get(
            "models",
            {},
        ).get(
            provider,
            self.DEFAULT_MODELS.get(
                provider,
                "",
            ),
        )

    def set_model(
        self,
        provider,
        model,
    ):
        provider = str(
            provider
        ).strip().lower()

        model = str(
            model
        ).strip()

        if not model:
            raise ValueError(
                "模型名称不能为空"
            )

        self.settings.setdefault(
            "models",
            {},
        )

        self.settings[
            "models"
        ][provider] = model

        self._save_settings()

    def _key_name(
        self,
        provider,
    ):
        provider = str(
            provider
        ).strip().lower()

        return f"api_key_{provider}"

    def get_api_key(
        self,
        provider=None,
    ):
        if provider is None:
            provider = (
                self.get_provider()
            )

        try:
            value = (
                keyring.get_password(
                    self.KEYRING_SERVICE,
                    self._key_name(
                        provider
                    ),
                )
            )

            return value or ""

        except Exception:
            return ""

    def set_api_key(
        self,
        provider,
        api_key,
    ):
        provider = str(
            provider
        ).strip().lower()

        api_key = str(
            api_key
        ).strip()

        if not api_key:
            self.delete_api_key(
                provider
            )
            return

        keyring.set_password(
            self.KEYRING_SERVICE,
            self._key_name(
                provider
            ),
            api_key,
        )

    def save(
        self,
        provider,
        model,
        api_key,
    ):
        provider = str(
            provider
        ).strip().lower()

        model = str(
            model
        ).strip()

        api_key = str(
            api_key
        ).strip()

        if provider not in (
            "deepseek",
            "openrouter",
        ):
            raise ValueError(
                "不支持的 AI 服务商"
            )

        if not model:
            raise ValueError(
                "模型名称不能为空"
            )

        self.settings[
            "provider"
        ] = provider

        self.settings.setdefault(
            "models",
            {},
        )

        self.settings[
            "models"
        ][provider] = model

        self._save_settings()

        if api_key:
            self.set_api_key(
                provider,
                api_key,
            )

    def delete_api_key(
        self,
        provider=None,
    ):
        if provider is None:
            provider = (
                self.get_provider()
            )

        try:
            keyring.delete_password(
                self.KEYRING_SERVICE,
                self._key_name(
                    provider
                ),
            )

        except Exception:
            pass

    def is_configured(
        self,
        provider=None,
    ):
        return bool(
            self.get_api_key(
                provider
            )
        )

    def get_theme(self):
        return self.settings.get(
            "appearance",
            {},
        ).get(
            "theme",
            "dark",
        )

    def set_theme(
        self,
        theme,
    ):
        self.settings.setdefault(
            "appearance",
            {},
        )

        self.settings[
            "appearance"
        ]["theme"] = theme

        self._save_settings()

    def get_voice_enabled(self):
        return bool(
            self.settings.get(
                "voice",
                {},
            ).get(
                "enabled",
                True,
            )
        )

    def set_voice_enabled(
        self,
        enabled,
    ):
        self.settings.setdefault(
            "voice",
            {},
        )

        self.settings[
            "voice"
        ]["enabled"] = bool(
            enabled
        )

        self._save_settings()

    def get_desktop_vision_enabled(self):
        """
        桌面视觉是否已由用户开启。

        fail-closed：只有配置里存的是【真正的 bool True】才返回 True。
        配置缺失、类型错误、损坏时一律返回 False。
        """

        section = self.settings.get(
            "desktop_vision",
            {},
        )

        if not isinstance(section, dict):

            return False

        return section.get("enabled") is True

    def set_desktop_vision_enabled(
        self,
        enabled,
    ):
        self.settings.setdefault(
            "desktop_vision",
            {},
        )

        self.settings[
            "desktop_vision"
        ]["enabled"] = bool(
            enabled
        )

        self._save_settings()

    def reload(self):
        self.settings = (
            self._load_settings()
        )

        return self.settings