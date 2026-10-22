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
            # 图片生成（R12.3-02 P3/F1：默认【关闭】，fail-closed）。
            #
            # 只有 enabled=True 且 服务商已注册 + 适配器受支持 +
            # API Key 存在 + 连接安全条件全部满足时才允许网络调用。
            # 任何前置条件失败 -> 零网络请求。
            "image_generation": {
                "enabled": False,
                "provider": "openai",
                "providers": {
                    "openai": {
                        "base_url": "https://api.openai.com/v1",
                        "protocol": "openai-images",
                        "model": "gpt-image-1",
                    },
                },
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

            # 图片生成：加法迁移 + fail-closed 解析（R12.3-02 C2/C4，
            # 不影响既有聊天提供商行为）。
            #
            # - enabled 只接受【真正的 bool True】；
            # - providers 只接受 dict，且每个 provider 只保留白名单内
            #   的键（base_url/protocol/model）与类型正确值；
            # - API Key 永不在此文件中保存（G2：仅 keyring）。
            _ig = loaded.get("image_generation")
            if isinstance(_ig, dict):
                _ig_enabled = _ig.get("enabled")
                if _ig_enabled is True:
                    defaults["image_generation"]["enabled"] = True
                _ig_provider = _ig.get("provider")
                if isinstance(_ig_provider, str) and _ig_provider.strip():
                    defaults["image_generation"]["provider"] = (
                        _ig_provider.strip().lower()
                    )
                _ig_providers = _ig.get("providers")
                if isinstance(_ig_providers, dict):
                    for pid, pconf in _ig_providers.items():
                        if not isinstance(pid, str) or not isinstance(
                            pconf, dict
                        ):
                            continue
                        cleaned = {}
                        _bu = pconf.get("base_url")
                        if isinstance(_bu, str) and _bu.startswith(
                            "https://"
                        ):
                            cleaned["base_url"] = _bu
                        _pr = pconf.get("protocol")
                        if isinstance(_pr, str) and _pr.strip():
                            cleaned["protocol"] = _pr.strip().lower()
                        _md = pconf.get("model")
                        if isinstance(_md, str) and _md.strip():
                            cleaned["model"] = _md.strip()
                        if cleaned:
                            defaults["image_generation"]["providers"][
                                pid.strip().lower()
                            ] = cleaned

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

    # ============================================================
    # 图片生成（R12.3-02 M2：统一服务商注册与白名单常量）
    # ============================================================
    #
    # 第一阶段只注册经过审查的 OpenAI 图片生成连接目标
    # （03 R2 / 04 方案 B）。其他服务商 / 未知域名 / 未知协议
    # 一律 CONNECT_TARGET_UNBOUND / UNSUPPORTED_PROTOCOL（05 C1）。
    #
    # API Key 不在此文件中保存（02 C4 / 03 R3，B1 决议）：
    # 唯一合法路径是 keyring（Windows 凭据管理器），键名
    # api_key_image_<provider>，由 SettingsManager.get_api_key
    # （keyring 路径，L281-340 区段）读写。

    IMAGE_PROVIDER_WHITELIST = {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "protocol": "openai-images",
            "model": "gpt-image-1",
        },
    }

    def get_image_generation_section(self):
        """fail-closed 读取 image_generation 配置块（加法迁移，C2）。

        缺失 / 损坏 / 类型错误时返回【干净的默认】（enabled=False），
        永不返回半初始化结构。
        """
        section = self.settings.get("image_generation")
        if not isinstance(section, dict):
            return self._default_settings()["image_generation"]

        out = {
            "enabled": section.get("enabled") is True,
            "provider": "openai",
            "providers": {},
        }
        prov = section.get("provider")
        if isinstance(prov, str) and prov.strip():
            out["provider"] = prov.strip().lower()
        providers = section.get("providers")
        if isinstance(providers, dict):
            for pid, pconf in providers.items():
                if not isinstance(pid, str) or not isinstance(pconf, dict):
                    continue
                cleaned = {}
                bu = pconf.get("base_url")
                if isinstance(bu, str) and bu.startswith("https://"):
                    cleaned["base_url"] = bu
                pr = pconf.get("protocol")
                if isinstance(pr, str) and pr.strip():
                    cleaned["protocol"] = pr.strip().lower()
                md = pconf.get("model")
                if isinstance(md, str) and md.strip():
                    cleaned["model"] = md.strip()
                if cleaned:
                    out["providers"][pid.strip().lower()] = cleaned
        return out

    def get_image_provider(self, provider_id):
        """统一服务商注册入口（02 P5）。

        白名单内 provider 合并注册值与白名单默认；未知 provider
        返回 None（适配器层据此抛 PROVIDER_NOT_REGISTERED /
        CONNECT_TARGET_UNBOUND，05 C1）。
        """
        provider_id = str(provider_id).strip().lower()
        whitelist = self.IMAGE_PROVIDER_WHITELIST.get(provider_id)
        if whitelist is None:
            return None

        section = self.get_image_generation_section()
        override = section["providers"].get(provider_id, {})

        merged = {
            "base_url": override.get(
                "base_url", whitelist["base_url"]
            ),
            "protocol": override.get(
                "protocol", whitelist["protocol"]
            ),
            "model": override.get(
                "model", whitelist["model"]
            ),
        }
        return merged

    def set_image_generation_enabled(self, enabled):
        """开关。fail-closed 落盘：只接受真正的 bool（C2/C4）。"""
        section = self.settings.setdefault(
            "image_generation",
            {},
        )
        section["enabled"] = enabled is True
        self._save_settings()

    def set_image_provider_config(self, provider_id, config):
        """更新白名单内 provider 的注册值（base_url/protocol/model）。

        未知 provider 拒绝（G3）；非法 URL（非 https）拒绝。
        不落盘任何密钥（G2）。
        """
        provider_id = str(provider_id).strip().lower()
        if provider_id not in self.IMAGE_PROVIDER_WHITELIST:
            raise ValueError(
                f"不支持的图片服务商：{provider_id}"
            )
        if not isinstance(config, dict):
            raise ValueError("config 必须是 dict")

        section = self.settings.setdefault(
            "image_generation",
            {},
        )
        providers = section.setdefault("providers", {})
        entry = providers.setdefault(provider_id, {})
        for key in ("base_url", "protocol", "model"):
            if key in config:
                value = config[key]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{key} 必须是非空字符串"
                    )
                if key == "base_url" and not value.startswith(
                    "https://"
                ):
                    raise ValueError(
                        "base_url 必须以 https:// 开头（G3/G4）"
                    )
                entry[key] = value.strip()
        self._save_settings()

    def get_image_api_key(self):
        """图片生成密钥：只从 keyring 读取（G2）。

        键名 api_key_image_<provider>，服务 AronaClient。
        缺失 / 读取失败 -> 空字符串（调用方判 CREDENTIAL_MISSING）。
        永不写入 settings.json。
        """
        provider_id = (
            self.get_image_generation_section().get(
                "provider",
                "openai",
            )
        )
        return self.get_api_key(f"image_{provider_id}")

    def set_image_api_key(self, api_key):
        """图片生成密钥：只写入 keyring（G2）。

        空值 = 删除（与 set_api_key 的语义一致）。
        """
        provider_id = (
            self.get_image_generation_section().get(
                "provider",
                "openai",
            )
        )
        self.set_api_key(f"image_{provider_id}", api_key)