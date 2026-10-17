import json
import uuid
from datetime import datetime
from pathlib import Path


class ConversationManager:
    """
    AronaClient 多会话管理器。

    会话数据保存到：
    %APPDATA%\\AronaClient\\conversations.json

    这样安装到 Program Files 等受保护目录后，
    普通 Windows 用户也可以正常保存聊天记录。
    """

    APP_NAME = "AronaClient"

    def __init__(self):
        self.data_dir = (
            Path.home()
            / "AppData"
            / "Roaming"
            / self.APP_NAME
        )
        self.data_file = self.data_dir / "conversations.json"

        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.data = self._load()

    def _now(self):
        return datetime.now().isoformat(timespec="seconds")

    def _load(self):
        if not self.data_file.exists():
            return {
                "current_id": None,
                "sessions": [],
            }

        try:
            with open(
                self.data_file,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if not isinstance(data, dict):
                raise ValueError("conversation data is invalid")

            if not isinstance(data.get("sessions"), list):
                data["sessions"] = []

            if "current_id" not in data:
                data["current_id"] = None

            return data

        except Exception:
            backup = self.data_file.with_name(
                "conversations.broken.json"
            )

            try:
                if self.data_file.exists():
                    self.data_file.replace(backup)
            except Exception:
                pass

            return {
                "current_id": None,
                "sessions": [],
            }

    def _save(self):
        self.data_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_file = self.data_file.with_suffix(".tmp")

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                self.data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        temp_file.replace(self.data_file)

    def list_sessions(self):
        sessions = self.data.get(
            "sessions",
            [],
        )

        return sorted(
            sessions,
            key=lambda item: item.get(
                "updated_at",
                "",
            ),
            reverse=True,
        )

    def get_current_id(self):
        return self.data.get("current_id")

    def set_current(self, session_id):
        if not self.get_session(session_id):
            return False

        self.data["current_id"] = session_id
        self._save()

        return True

    def create_session(self, title="新对话"):
        now = self._now()

        session = {
            "id": str(uuid.uuid4()),
            "title": title,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

        self.data.setdefault(
            "sessions",
            [],
        )

        self.data["sessions"].insert(
            0,
            session,
        )

        self.data["current_id"] = session["id"]

        self._save()

        return session

    def get_session(self, session_id):
        for session in self.data.get(
            "sessions",
            [],
        ):
            if session.get("id") == session_id:
                return session

        return None

    def update_messages(
        self,
        session_id,
        messages,
        title=None,
    ):
        session = self.get_session(session_id)

        if not session:
            return False

        session["messages"] = list(messages)

        if title is not None:
            title = str(title).strip()

            if title:
                session["title"] = title

        session["updated_at"] = self._now()
        self.data["current_id"] = session_id

        self._save()

        return True

    def rename_session(
        self,
        session_id,
        title,
    ):
        session = self.get_session(session_id)

        if not session:
            return False

        title = str(title).strip()

        if not title:
            title = "新对话"

        session["title"] = title
        session["updated_at"] = self._now()

        self._save()

        return True

    def delete_session(self, session_id):
        sessions = self.data.get(
            "sessions",
            [],
        )

        new_sessions = [
            session
            for session in sessions
            if session.get("id") != session_id
        ]

        if len(new_sessions) == len(sessions):
            return False

        self.data["sessions"] = new_sessions

        if self.data.get("current_id") == session_id:
            if new_sessions:
                self.data["current_id"] = new_sessions[0].get(
                    "id"
                )
            else:
                self.data["current_id"] = None

        self._save()

        return True

    def clear_session(self, session_id):
        session = self.get_session(session_id)

        if not session:
            return False

        session["messages"] = []
        session["title"] = "新对话"
        session["updated_at"] = self._now()

        self.data["current_id"] = session_id

        self._save()

        return True

    def clear_all(self):
        self.data = {
            "current_id": None,
            "sessions": [],
        }

        self._save()

    def ensure_session(self):
        sessions = self.list_sessions()

        if sessions:
            current_id = self.get_current_id()

            if current_id:
                current = self.get_session(
                    current_id
                )

                if current:
                    return current

            session = sessions[0]

            self.data["current_id"] = session.get("id")

            self._save()

            return session

        return self.create_session()