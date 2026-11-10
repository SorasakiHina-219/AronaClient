"""Preview runtime — child process that displays the real Spatial UI.

Isolation:
  * AIClient        -> PreviewAIClient   (offline canned replies)
  * SettingsManager -> PreviewSettings   (isolated json)
  * window_manager  -> settings_path=data/preview/window_geometry.json
  * UI_STATE_FILE   -> data/preview/spatial_ui_state.json
  * No keyring, no network, no ComputerController, no desktop vision.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.spatial_preview import preview_config as cfg
from tools.spatial_preview.display_manager import (
    DisplayInfo,
    ensure_on_screen,
    enumerate_displays,
    move_window_to_display,
)


def log(msg: str) -> None:
    cfg.ensure_dirs()
    line = '[' + time.strftime('%Y-%m-%d %H:%M:%S') + '] ' + msg
    print(line, flush=True)
    try:
        with open(cfg.PREVIEW_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except OSError:
        pass


def write_status(state: str, **extra) -> None:
    cfg.write_status({"state": state, **extra})


class PreviewAIClient:
    """Offline AIClient stand-in. No network, no keyring, no ComputerController."""

    CANNED_REPLY = (
        "preview mode: received your message.\n"
        "This is the Spatial UI TV preview environment;\n"
        "the AI backend is fully offline.\n"
    )

    def __init__(self, data_dir: Path = cfg.PREVIEW_DATA_DIR):
        self._data_dir = data_dir
        self.provider = "deepseek"
        self.model = "preview-mode"
        self.messages: list = []
        self.current_session = None
        self._sessions = self._load_sessions()

    def get_provider(self) -> str:
        return self.provider

    def set_provider(self, provider: str) -> None:
        if provider in ("deepseek", "openrouter"):
            self.provider = provider

    def get_model(self) -> str:
        return self.model

    def set_model(self, model: str) -> None:
        if model:
            self.model = model

    def is_configured(self) -> bool:
        return True

    def chat(self, message: str, **_kwargs):
        self.messages.append({"role": "user", "content": message})
        reply = self.CANNED_REPLY
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    # T1 兼容别名：L2 主窗已改为调用正式 AIClient API（send_message /
    # create_conversation，见 spatial_main_window.py _AIWorker / new_chat）。
    # 预览 mock 同时保留 chat / new_session（旧路径）与下列别名（新路径），
    # 使预览工具与正式入口共用同一套 L2 业务代码。
    def send_message(self, message, attachments=None, save_to_session=True):
        return self.chat(message)

    def create_conversation(self, title="新对话"):
        return self.new_session()

    def clear_history(self) -> None:
        self.messages = []

    def _sessions_file(self) -> Path:
        return self._data_dir / "preview_sessions.json"

    def _load_sessions(self) -> dict:
        p = self._sessions_file()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_sessions(self) -> None:
        try:
            p = self._sessions_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(self._sessions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def list_conversations(self) -> list:
        return [
            {"id": sid, "title": s.get("title", "Preview")}
            for sid, s in self._sessions.items()
        ]

    def new_session(self) -> dict:
        sid = "pv_%d" % int(time.time() * 1000)
        session = {
            "id": sid,
            "title": "Preview",
            "created_at": time.time(),
            "messages": [],
        }
        self._sessions[sid] = session
        self.current_session = session
        self._save_sessions()
        return session

    def get_current_session(self):
        return self.current_session

    def switch_conversation(self, session_id: str) -> bool:
        s = self._sessions.get(session_id)
        if not s:
            return False
        self.current_session = s
        return True


class PreviewSettings:
    """Isolated SettingsManager stand-in. Persists to preview_settings.json only."""

    def __init__(self, data_dir: Path = cfg.PREVIEW_DATA_DIR):
        self._data_dir = data_dir
        self.SETTINGS_FILE = data_dir / "preview_settings.json"
        self.settings = self._load()

    def _load(self) -> dict:
        p = self.SETTINGS_FILE
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"spatial_ui": {"model_runtime_enabled": True}}

    def _save_settings(self, _data=None) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self.SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def read_command():
    p = cfg.PREVIEW_COMMAND_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        p.unlink(missing_ok=True)
        return data
    except Exception:
        return None


def patch_ui_state_file() -> None:
    import spatial_ui.spatial_main_window as smw
    smw.UI_STATE_FILE = cfg.PREVIEW_UI_STATE_FILE


def install_import_stubs(ai=None, settings=None) -> None:
    """Make "from core.ai_client import AIClient" resolve to offline stubs.

    spatial_ui.spatial_main_window imports AIClient / SettingsManager at
    module scope. Importing the real core.ai_client pulls in cv2, openai,
    PyAutoGUI and the computer-control stack — slow (30s+ on a loaded
    laptop) and full of side effects we must not trigger in preview mode.

    Installing lightweight module objects in sys.modules keeps the real
    modules completely unimported: no keyring, no network clients, no
    desktop-control controllers, and much faster startup.
    """
    import types

    ai = ai or PreviewAIClient()
    settings = settings or PreviewSettings()

    if "core.ai_client" not in sys.modules:
        m = types.ModuleType("core.ai_client")
        # callable that returns the SAME stub instance every time
        m.AIClient = _StubFactory(lambda: ai)
        sys.modules["core.ai_client"] = m

    if "core.settings" not in sys.modules:
        m2 = types.ModuleType("core.settings")
        m2.SettingsManager = _StubFactory(lambda: settings)
        sys.modules["core.settings"] = m2


class _StubFactory:
    """Callable stand-in for a class: returns a fixed instance."""

    __slots__ = ("_maker",)

    def __init__(self, maker):
        self._maker = maker

    def __call__(self, *args, **kwargs):
        return self._maker()

    def __repr__(self) -> str:
        return "<preview stub factory>"


def _try_hot_style_reload(window, perf_mode: str) -> None:
    try:
        import importlib
        import spatial_ui.styles as styles
        import spatial_ui.theme as theme
        importlib.reload(styles)
        importlib.reload(theme)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_app_qss(performance_mode=perf_mode))
        window._apply_app_theme()
        log("style hot-reload applied")
    except Exception as exc:
        log("style hot-reload failed: %s" % exc)


def _try_resource_reload(window) -> None:
    try:
        stage = getattr(window, "home_page", None)
        if stage is not None:
            stage_widget = getattr(stage, "stage", None)
            if stage_widget is not None and hasattr(stage_widget, "_load_optional_image"):
                stage_widget._load_optional_image()
                stage_widget.update()
        log("resource reload done")
    except Exception as exc:
        log("resource reload failed: %s" % exc)


def main() -> int:
    display_target = os.environ.get("ARONA_PREVIEW_DISPLAY")
    fullscreen = os.environ.get("ARONA_PREVIEW_FULLSCREEN", "1") != "0"
    perf_mode = os.environ.get("ARONA_PREVIEW_PERF", "medium")

    cfg.ensure_dirs()

    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AronaClient Preview")

    # Install offline module stubs BEFORE importing the window module:
    # this avoids importing cv2/openai/pyautogui/keyring entirely.
    ai = PreviewAIClient()
    settings = PreviewSettings()
    install_import_stubs(ai=ai, settings=settings)

    patch_ui_state_file()

    t_import = time.time()
    from spatial_ui.window_manager import SpatialWindowManager
    from spatial_ui.spatial_main_window import SpatialMainWindow
    log("window modules imported in %.1fs" % (time.time() - t_import))

    wm = SpatialWindowManager(
        performance_mode=perf_mode,
        settings_path=cfg.PREVIEW_WIN_GEOM_FILE,
    )

    log("building SpatialMainWindow (perf=%s)" % perf_mode)
    window = SpatialMainWindow(
        performance_mode=perf_mode,
        ai_client=ai,
        settings=settings,
        window_manager=wm,
    )

    write_status("placing", display_target=display_target, fullscreen=fullscreen)
    target_info = move_window_to_display(window, target=display_target, fullscreen=fullscreen)
    if target_info is None:
        log("no target display; showing on current screen")
        window.show()
        write_status("running", display="fallback")
    else:
        log("window on display %d %s %s" % (target_info.index, target_info.name, target_info.resolution))
        write_status(
            "running",
            display_index=target_info.index,
            display_name=target_info.name,
            resolution=target_info.resolution,
        )

    def _display_signature(infos):
        """Stable signature of the current display set (names + geometry)."""
        return tuple(
            (d.index, d.name, d.geometry[2], d.geometry[3], d.is_primary)
            for d in infos
        )

    class _Hotplug(QObject):
        def __init__(self):
            super().__init__()
            self._current = target_info
            self._last_sig = _display_signature(enumerate_displays())

        def on_screens_changed(self, _=None):
            # Poll-only: act ONLY when the display set / geometry actually
            # changed (avoids repositioning the window every poll).
            try:
                infos = enumerate_displays()
                if not infos:
                    return
                sig = _display_signature(infos)
                if sig == self._last_sig:
                    return
                self._last_sig = sig
                log("display configuration changed (%d display(s))" % len(infos))

                if self._current is not None:
                    for d in infos:
                        if d.index == self._current.index and d.name == self._current.name:
                            log("preferred display present: %s; repositioning" % d.name)
                            move_window_to_display(window, target=str(d.index),
                                                   fullscreen=fullscreen)
                            write_status("running", display_index=d.index,
                                         display_name=d.name,
                                         resolution=d.resolution)
                            return
                log("preferred display gone; falling back to a valid display")
                fallback = move_window_to_display(window, target=None,
                                                  fullscreen=fullscreen)
                self._current = fallback
                if fallback is not None:
                    write_status("running", display_index=fallback.index,
                                 display_name=fallback.name,
                                 resolution=fallback.resolution,
                                 note="display-fallback")
            except Exception as exc:
                log("hotplug error: %s" % exc)

    hotplug = _Hotplug()
    # Use a timer-based poll instead of screensChanged signal (more portable).
    _hotplug_timer = QTimer()
    _hotplug_timer.setInterval(3000)
    _hotplug_timer.timeout.connect(hotplug.on_screens_changed)
    _hotplug_timer.start()

    def poll_commands():
        cmd = read_command()
        if not cmd:
            return
        kind = cmd.get("cmd")
        log("command: %s" % kind)
        if kind == "shutdown":
            write_status("stopping")
            QTimer.singleShot(50, app.quit)
        elif kind == "restart":
            write_status("restarting")
            QTimer.singleShot(50, app.quit)
        elif kind == "reload_style":
            _try_hot_style_reload(window, perf_mode)
        elif kind == "reload_resources":
            _try_resource_reload(window)

    cmd_timer = QTimer()
    cmd_timer.setInterval(600)
    cmd_timer.timeout.connect(poll_commands)
    cmd_timer.start()

    def _on_sigterm(*_):
        log("SIGTERM")
        write_status("stopping")
        QTimer.singleShot(30, app.quit)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_sigterm)

    write_status("running", pid=os.getpid())
    log("event loop started")
    exit_code = app.exec()
    log("event loop exited (code=%d)" % exit_code)
    write_status("stopped", exit_code=exit_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
