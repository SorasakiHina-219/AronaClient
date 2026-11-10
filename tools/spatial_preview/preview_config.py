"""Preview environment configuration and isolated paths.

All preview data is confined to <repo>/data/preview/ so that the preview
never touches the real user state under AppData or keyring.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

PREVIEW_DATA_DIR = PROJECT_ROOT / "data" / "preview"
PREVIEW_LOG_DIR = PROJECT_ROOT / "logs" / "preview"

PREVIEW_SETTINGS_FILE = PREVIEW_DATA_DIR / "preview_settings.json"
PREVIEW_UI_STATE_FILE = PREVIEW_DATA_DIR / "spatial_ui_state.json"
PREVIEW_WIN_GEOM_FILE = PREVIEW_DATA_DIR / "window_geometry.json"
PREVIEW_STATUS_FILE = PREVIEW_DATA_DIR / "status.json"
PREVIEW_COMMAND_FILE = PREVIEW_DATA_DIR / "command.json"
PREVIEW_LOG_FILE = PREVIEW_LOG_DIR / "preview.log"
SUP_PID_FILE = PREVIEW_DATA_DIR / "supervisor.pid"

# File classification rules for hot-reload strategy
STYLE_FILES = [
    "spatial_ui/styles.py",
    "spatial_ui/theme.py",
    "spatial_ui/animations.py",
]
RESOURCE_DIRS = [
    "assets/",
]
LAYOUT_FILES = [
    "spatial_ui/sidebar.py",
    "spatial_ui/topbar.py",
    "spatial_ui/cards.py",
    "spatial_ui/floating_menu.py",
    "spatial_ui/composer.py",
    "spatial_ui/glass_panel.py",
    "spatial_ui/chat_fade.py",
    "spatial_ui/bubble.py",
    "spatial_ui/space_background.py",
]
CONFIG_FILES = [
    "config.py",
    "requirements.txt",
]
LOGIC_FILES = [
    "spatial_ui/spatial_main_window.py",
    "spatial_ui/model_runtime.py",
    "spatial_ui/model_stage.py",
    "spatial_ui/window_manager.py",
    "spatial_ui/__init__.py",
    "spatial_ui/__main__.py",
    "mascot_3d.py",
    "core/settings.py",
]
WATCH_GLOBS = [
    "spatial_ui/**/*.py",
    "assets/**/*.png",
    "assets/**/*.jpg",
    "assets/**/*.webp",
    "assets/**/*.ico",
    "mascot_3d.py",
    "core/settings.py",
    "config.py",
]


def ensure_dirs() -> None:
    PREVIEW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_LOG_DIR.mkdir(parents=True, exist_ok=True)


def classify_change(rel_path: str) -> str:
    """Classify a changed file into a hot-reload strategy.

    Returns one of: style, resource, layout, logic, config, ignore
    """
    rp = rel_path.replace("\\", "/")
    if any(rp == f for f in STYLE_FILES):
        return "style"
    if any(rp == f for f in LAYOUT_FILES):
        return "layout"
    if any(rp == f for f in CONFIG_FILES):
        return "config"
    if any(rp == f for f in LOGIC_FILES):
        return "logic"
    for d in RESOURCE_DIRS:
        if rp.startswith(d):
            return "resource"
    if rp.startswith("spatial_ui/") and rp.endswith(".py"):
        return "layout"
    return "ignore"


def strategy_for(kind: str) -> str:
    return {
        "style": "hot_reload_style",
        "resource": "reload_resources",
        "layout": "rebuild_ui",
        "logic": "restart_process",
        "config": "check_and_notify",
        "ignore": "ignore",
    }[kind]


def read_status() -> Optional[Dict]:
    if PREVIEW_STATUS_FILE.exists():
        try:
            return json.loads(PREVIEW_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def write_status(state_or_payload, *args, **kwargs) -> None:
    """Write status.json.

    Two calling forms supported:
      write_status({"state": "running", ...})
      write_status("running", key=value, ...)
    """
    ensure_dirs()
    if isinstance(state_or_payload, dict):
        payload = dict(state_or_payload)
    else:
        payload = {"state": state_or_payload}
        payload.update(kwargs)
    payload["ts"] = time.time()
    tmp = PREVIEW_STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(PREVIEW_STATUS_FILE)


def write_command(cmd: str, **extra) -> None:
    ensure_dirs()
    payload = {"cmd": cmd, "seq": int(extra.pop("seq", 0))}
    payload.update(extra)
    tmp = PREVIEW_COMMAND_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(PREVIEW_COMMAND_FILE)
