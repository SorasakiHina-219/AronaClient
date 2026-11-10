"""Display enumeration and cross-screen window positioning.

All functions here work in two layers:
  * Pure helpers (DisplayInfo, pick_display, place_window_fullscreen) — no Qt required,
    easy to unit-test.
  * Qt integration helpers (enumerate_displays, select_display, move_window_to_display) —
    require a running QApplication / QGuiApplication.

The goal is to reliably move the preview window onto a non-primary display
(e.g. a Sony TV) using that display's own geometry, not the primary screen's
coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Optional

from PySide6.QtCore import QRect, QPoint, Qt
from PySide6.QtGui import QGuiApplication, QScreen


@dataclass
class DisplayInfo:
    """Description of a single display, independent of Qt objects."""
    index: int
    name: str
    is_primary: bool
    geometry: tuple  # (x, y, w, h) global virtual-desktop coords
    available_geometry: tuple  # (x, y, w, h) excluding taskbar/dock

    @property
    def resolution(self) -> str:
        return f"{self.geometry[2]}x{self.geometry[3]}"

    def to_dict(self) -> dict:
        return asdict(self)


def enumerate_displays() -> List[DisplayInfo]:
    """Return all connected displays as pure data."""
    app = QGuiApplication.instance()
    if app is None:
        raise RuntimeError("No running QGuiApplication; call this from a Qt app context")
    primary = app.primaryScreen()
    infos: List[DisplayInfo] = []
    for i, screen in enumerate(app.screens()):
        g = screen.geometry()
        ag = screen.availableGeometry()
        infos.append(
            DisplayInfo(
                index=i,
                name=screen.name(),
                is_primary=(screen is primary),
                geometry=(g.x(), g.y(), g.width(), g.height()),
                available_geometry=(ag.x(), ag.y(), ag.width(), ag.height()),
            )
        )
    return infos


def pick_display(infos: List[DisplayInfo], target: Optional[str] = None) -> Optional[DisplayInfo]:
    """Pick a target display.

    target:
      None           -> auto: first non-primary display (largest area), else primary
      "index"        -> index into the screens() list (e.g. "1")
      substring/name -> case-insensitive substring match against the display name
    """
    if not infos:
        return None
    if target is None or target.lower() == "auto":
        # Prefer the largest non-primary display
        nonprimary = [d for d in infos if not d.is_primary]
        if nonprimary:
            nonprimary.sort(key=lambda d: d.geometry[2] * d.geometry[3], reverse=True)
            return nonprimary[0]
        return infos[0]

    t = target.strip()
    if t.isdigit():
        idx = int(t)
        for d in infos:
            if d.index == idx:
                return d
        return None
    tl = t.lower()
    for d in infos:
        if tl in d.name.lower():
            return d
    return None


def _screen_for_index(index: int) -> Optional[QScreen]:
    screens = QGuiApplication.screens()
    if 0 <= index < len(screens):
        return screens[index]
    return None


def move_window_to_display(window, target: Optional[str] = None, fullscreen: bool = True) -> Optional[DisplayInfo]:
    """Move ``window`` to the target display and optionally make it fullscreen there.

    Uses screen-moved approach: position the window on the target screen's
    coordinate space, then trigger fullscreen/maximized so the WM keeps it
    on that screen.

    Returns the DisplayInfo of the display the window was actually placed on,
    or None if no display could be selected.
    """
    infos = enumerate_displays()
    info = pick_display(infos, target)
    if info is None:
        return None
    screen = _screen_for_index(info.index)
    if screen is None:
        # fall back to first available screen matching by name
        for s in QGuiApplication.screens():
            if info.name and info.name.lower() in s.name().lower():
                screen = s
                break
    if screen is None:
        return None

    ag = QRect(*info.available_geometry)
    geo = QRect(*info.geometry)

    # Step 1: put the window (still windowed) onto the target screen's
    # coordinate space. Qt decides which screen a fullscreen window uses
    # based on where the window currently is, so this must happen first.
    window.show()
    window.setWindowState(Qt.WindowNoState)
    if fullscreen:
        # A small probe geometry inside the target screen's upper-left corner.
        # (Do NOT use availableGeometry here: it would leave the taskbar
        # visible after showFullScreen() because we re-apply geometry later.)
        window.setGeometry(geo.x() + 40, geo.y() + 40, 800, 600)
    else:
        window.setGeometry(ag)

    # Step 2: apply the final window state.
    if fullscreen:
        window.showFullScreen()
        # Some Windows compositors move a freshly-fullscreened window to the
        # primary screen; if that happened, re-home it onto the target screen
        # and re-apply fullscreen once.
        try:
            current = window.screen()
            if current is not None and current.name() != info.name:
                window.setWindowState(Qt.WindowNoState)
                window.setGeometry(geo.x() + 40, geo.y() + 40, 800, 600)
                window.showFullScreen()
        except Exception:
            pass
    else:
        window.showMaximized()

    return info


def ensure_on_screen(window, display: DisplayInfo) -> None:
    """Clamp the window into the display's geometry (used after resolution changes)."""
    g = QRect(*display.geometry)
    w = min(window.width(), g.width())
    h = min(window.height(), g.height())
    x = max(g.x(), min(window.x(), g.right() - w))
    y = max(g.y(), min(window.y(), g.bottom() - h))
    window.setGeometry(x, y, w, h)
