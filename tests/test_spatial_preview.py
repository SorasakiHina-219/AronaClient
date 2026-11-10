"""Unit tests for the spatial preview system (pure logic, no Qt display needed).

Run:
  .venv/Scripts/python.exe -m pytest tests/test_spatial_preview.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.spatial_preview import preview_config as cfg
from tools.spatial_preview.display_manager import DisplayInfo, pick_display
from tools.spatial_preview.file_watcher import Debouncer, classify_change, strategy_for
from tools.spatial_preview.preview_runtime import (
    PreviewAIClient,
    PreviewSettings,
)


# ── display selection ─────────────────────────────────────────────────

def _sample_displays():
    return [
        DisplayInfo(
            index=0, name="\\.\\DISPLAY1", is_primary=True,
            geometry=(0, 0, 1366, 768),
            available_geometry=(0, 0, 1366, 730),
        ),
        DisplayInfo(
            index=1, name="\\.\\DISPLAY26", is_primary=False,
            geometry=(1366, 0, 1920, 1080),
            available_geometry=(1366, 0, 1920, 1040),
        ),
    ]


def test_pick_auto_prefers_largest_nonprimary():
    chosen = pick_display(_sample_displays(), None)
    assert chosen is not None
    assert chosen.index == 1
    assert chosen.name.endswith("DISPLAY26")


def test_pick_by_index():
    chosen = pick_display(_sample_displays(), "1")
    assert chosen is not None
    assert chosen.index == 1


def test_pick_by_name_substring():
    chosen = pick_display(_sample_displays(), "display26")
    assert chosen is not None
    assert chosen.index == 1


def test_pick_by_name_primary():
    chosen = pick_display(_sample_displays(), "display1")
    assert chosen is not None
    assert chosen.index == 0


def test_pick_no_match_returns_none():
    assert pick_display(_sample_displays(), "nonexistent") is None


def test_pick_single_display_fallback():
    single = [
        DisplayInfo(index=0, name="\\.\\DISPLAY1", is_primary=True,
                    geometry=(0, 0, 1920, 1080),
                    available_geometry=(0, 0, 1920, 1040)),
    ]
    chosen = pick_display(single, None)
    assert chosen is not None
    assert chosen.index == 0


# ── debouncer ────────────────────────────────────────────────────────

def test_debouncer_coalesces_burst():
    fired = []
    d = Debouncer(quiet_ms=50, on_fire=fired.append)
    import time as _t
    d.event("a.py")
    d.event("b.py")
    d.event("a.py")
    _t.sleep(0.08)
    flushed = d.poll()
    assert flushed == ["a.py", "b.py"]
    assert fired == [["a.py", "b.py"]]


def test_debouncer_no_event_no_flush():
    d = Debouncer(quiet_ms=50)
    assert d.poll() is None
    assert d.pending() == []


def test_debouncer_stale_flush_preserves_events():
    """If quiet window elapses while events pending, they flush first
    before a new quiet window starts (no lost events)."""
    fired = []
    d = Debouncer(quiet_ms=30, on_fire=fired.append)
    import time as _t
    d.event("x.py")
    _t.sleep(0.06)          # quiet window elapsed
    d.event("y.py")        # should flush x first, then start fresh window
    assert fired[-1] == ["x.py"]
    _t.sleep(0.06)
    assert d.poll() == ["y.py"]


# ── change classification ────────────────────────────────────────────

@pytest.mark.parametrize("rel,expected", [
    ("spatial_ui/styles.py", "style"),
    ("spatial_ui/theme.py", "style"),
    ("spatial_ui/animations.py", "style"),
    ("spatial_ui/sidebar.py", "layout"),
    ("spatial_ui/cards.py", "layout"),
    ("spatial_ui/composer.py", "layout"),
    ("spatial_ui/spatial_main_window.py", "logic"),
    ("spatial_ui/model_runtime.py", "logic"),
    ("spatial_ui/window_manager.py", "logic"),
    ("mascot_3d.py", "logic"),
    ("core/settings.py", "logic"),
    ("config.py", "config"),
    ("assets/3d/arona/foo.png", "resource"),
    ("assets/icons/x.ico", "resource"),
    ("some/other/file.py", "ignore"),
])
def test_classify(rel, expected):
    assert classify_change(rel) == expected


def test_strategy_mapping():
    assert strategy_for("style") == "hot_reload_style"
    assert strategy_for("resource") == "reload_resources"
    assert strategy_for("layout") == "rebuild_ui"
    assert strategy_for("logic") == "restart_process"
    assert strategy_for("config") == "check_and_notify"
    assert strategy_for("ignore") == "ignore"


# ── mock backends (isolation) ─────────────────────────────────────────

def test_preview_ai_client_makes_no_network(tmp_path):
    ai = PreviewAIClient(data_dir=tmp_path)
    assert ai.is_configured() is True
    reply = ai.chat("hello")
    assert reply is not None
    assert "预览" in reply or "preview" in reply.lower()
    # chat stores messages locally
    assert len(ai.messages) == 2


def test_preview_ai_client_sessions(tmp_path):
    ai = PreviewAIClient(data_dir=tmp_path)
    s = ai.new_session()
    assert s["id"].startswith("pv_")
    convs = ai.list_conversations()
    assert any(c["id"] == s["id"] for c in convs)
    assert ai.switch_conversation(s["id"]) is True
    assert ai.current_session is s


def test_preview_settings_isolated(tmp_path):
    s = PreviewSettings(data_dir=tmp_path)
    assert s.settings["spatial_ui"]["model_runtime_enabled"] is True
    s.settings["spatial_ui"]["model_runtime_enabled"] = False
    s._save_settings()
    # a fresh manager reads the persisted value
    s2 = PreviewSettings(data_dir=tmp_path)
    assert s2.settings["spatial_ui"]["model_runtime_enabled"] is False


def test_status_write_read_roundtrip(tmp_path, monkeypatch):
    # point the module-level files at tmp_path
    monkeypatch.setattr(cfg, "PREVIEW_STATUS_FILE", tmp_path / "status.json")
    cfg.write_status({"state": "running", "display_index": 1})
    got = cfg.read_status()
    assert got["state"] == "running"
    assert got["display_index"] == 1


def test_command_write(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PREVIEW_COMMAND_FILE", tmp_path / "command.json")
    cfg.write_command("shutdown", seq=1, reason="test")
    got = cfg.PREVIEW_COMMAND_FILE.read_text()
    import json as _json
    data = _json.loads(got)
    assert data["cmd"] == "shutdown"
    assert data["seq"] == 1
