import json
from pathlib import Path


class MemoryManager:
    """
    AronaClient 长期记忆管理器。

    记忆保存到：
    %APPDATA%\\AronaClient\\memories.json

    不会随着聊天会话删除。
    """

    APP_NAME = "AronaClient"

    def __init__(self):
        self.data_dir = (
            Path.home()
            / "AppData"
            / "Roaming"
            / self.APP_NAME
        )

        self.data_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.memory_file = (
            self.data_dir / "memories.json"
        )

        self.memories = self._load()

    def _load(self):
        if not self.memory_file.exists():
            return []

        try:
            with open(
                self.memory_file,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if isinstance(data, list):
                return data

        except Exception:
            pass

        return []

    def _save(self):
        self.data_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_file = self.memory_file.with_suffix(
            ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                self.memories,
                f,
                ensure_ascii=False,
                indent=2,
            )

        temp_file.replace(
            self.memory_file
        )

    def add_memory(self, content):
        content = (content or "").strip()

        if not content:
            return False

        if content in self.memories:
            return False

        self.memories.append(content)

        self._save()

        return True

    def remove_memory(self, content):
        content = (content or "").strip()

        if content not in self.memories:
            return False

        self.memories.remove(content)

        self._save()

        return True

    def get_all(self):
        return list(self.memories)

    def clear(self):
        self.memories = []

        self._save()

    def build_memory_context(self):
        if not self.memories:
            return ""

        memory_text = "\n".join(
            f"- {memory}"
            for memory in self.memories
        )

        return (
            "以下是你与用户长期交流中保存的记忆。\n"
            "这些信息可以用于后续聊天，但不要凭空扩展、"
            "修改或捏造这些事实：\n\n"
            f"{memory_text}"
        )

    def auto_remember(self, user_message):
        """
        从用户消息中识别明确要求长期记住的信息。

        当前版本使用本地规则，不额外调用 AI，
        因此不会增加 API 消耗。
        """

        text = (user_message or "").strip()

        if not text:
            return []

        patterns = [
            "请记住",
            "记住我",
            "记住：",
            "记住:",
            "我的名字是",
            "我叫",
            "我喜欢",
            "我不喜欢",
            "我最喜欢",
            "我的生日是",
        ]

        should_remember = any(
            pattern in text
            for pattern in patterns
        )

        if not should_remember:
            return []

        remembered = []

        if self.add_memory(text):
            remembered.append(text)

        return remembered