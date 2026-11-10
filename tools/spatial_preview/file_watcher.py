"""File-change detection for the preview hot-reload loop.

Uses QFileSystemWatcher for directory-level notifications on Windows,
verified against mtime (QFileSystemWatcher on Windows only supports
file-level watching; directory watching works but can emit stale
events, so every callback re-checks mtime).

Pure helper ``Debouncer`` is Qt-free and unit-testable.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from tools.spatial_preview.preview_config import classify_change, strategy_for


class Debouncer:
    """Qt-free debounce: calls ``on_fire(changed)`` only after ``quiet_ms``
    of no new events, coalescing all events in the window.

    ``changed`` is a list of relative paths that fired since the last flush.
    """

    def __init__(self, quiet_ms: int = 800, on_fire=None):
        self.quiet_ms = quiet_ms
        self.on_fire = on_fire
        self._pending: Set[str] = set()
        self._last_event_ts = 0.0

    def event(self, rel_path: str) -> None:
        now = time.time()
        if self._pending and (now - self._last_event_ts) * 1000 > self.quiet_ms:
            # Quiescent window already elapsed while we had pending events —
            # flush first so nothing is lost, then start a fresh window.
            self._flush_locked()
        self._pending.add(rel_path)
        self._last_event_ts = now

    def poll(self) -> None:
        """Called by a timer; flushes when quiet. Returns list flushed (or None)."""
        if not self._pending:
            return None
        if (time.time() - self._last_event_ts) * 1000 >= self.quiet_ms:
            return self._flush_locked()
        return None

    def _flush_locked(self):
        items = sorted(self._pending)
        self._pending.clear()
        self._last_event_ts = 0.0
        if self.on_fire:
            self.on_fire(items)
        return items

    def pending(self) -> List[str]:
        return sorted(self._pending)


def file_signature(paths: List[Path]) -> str:
    """Stable signature of a set of files (mtime+size), used to detect
    whether a restart target changed since the last attempt (infinite-loop guard)."""
    sig = []
    for p in sorted(paths, key=str):
        try:
            st = p.stat()
            sig.append(f"{p.name}:{int(st.st_mtime * 1000)}:{st.st_size}")
        except OSError:
            sig.append(f"{p.name}:missing")
    return "|".join(sig)


class PreviewFileWatcher(QObject):
    """Watches a set of directories/files and emits debounced change batches.

    change_batch = Signal(List[str])   — relative paths that changed
    """

    change_batch = Signal(list)

    def __init__(
        self,
        project_root: Path,
        watch_globs: List[str],
        quiet_ms: int = 800,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._root = Path(project_root)
        self._watch_globs = watch_globs
        self._debouncer = Debouncer(quiet_ms, on_fire=self._emit_batch)
        self._watcher = None
        self._known_files: Dict[Path, float] = {}
        self._scan_timer: Optional[QTimer] = None

    # ── scanning ────────────────────────────────────────────────────────

    def _match_files(self) -> List[Path]:
        found: List[Path] = []
        for pat in self._watch_globs:
            found.extend(list(self._root.glob(pat)))
        return [p.resolve() for p in found if p.is_file()]

    def _snapshot(self) -> Dict[Path, float]:
        out: Dict[Path, float] = {}
        for p in self._match_files():
            try:
                out[p] = p.stat().st_mtime
            except OSError:
                pass
        return out

    # ── wiring ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start watching. Uses QFileSystemWatcher on watched files plus a
        slow polling timer as a safety net (file creation is not always
        notified on Windows)."""
        from PySide6.QtCore import QFileSystemWatcher

        self._known_files = self._snapshot()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.addPaths([str(p) for p in self._known_files])

        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(2000)
        self._scan_timer.timeout.connect(self._poll_scan)
        self._scan_timer.start()

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setInterval(max(50, min(500, self._debouncer.quiet_ms // 4)))
        self._debounce_timer.timeout.connect(self._poll_debounce)
        self._debounce_timer.start()

    def stop(self) -> None:
        for t in (getattr(self, "_scan_timer", None), getattr(self, "_debounce_timer", None)):
            if t is not None:
                t.stop()
        if self._watcher is not None:
            try:
                self._watcher.fileChanged.disconnect(self._on_file_changed)
            except (RuntimeError, TypeError):
                pass

    def _on_file_changed(self, _path: str) -> None:
        self._poll_scan()

    def _poll_scan(self) -> None:
        snap = self._snapshot()
        prev = self._known_files
        changed: Set[str] = set()
        for p in snap:
            rel = self._rel(p)
            if p not in prev or prev[p] != snap[p]:
                changed.add(rel)
        # also detect deletions
        for p in prev:
            if p not in snap:
                changed.add(self._rel(p))
        # keep watcher in sync with the new set of files
        if self._watcher is not None:
            current_paths = [str(p) for p in snap]
            wanted = set(current_paths)
            existing = set(self._watcher.files())
            for gone in existing - wanted:
                try:
                    self._watcher.removePath(gone)
                except Exception:
                    pass
            for new in wanted - existing:
                try:
                    self._watcher.addPath(new)
                except Exception:
                    pass
        self._known_files = snap
        for rel in changed:
            self._debouncer.event(rel)

    def _poll_debounce(self) -> None:
        self._debouncer.poll()

    def _emit_batch(self, items: List[str]) -> None:
        self.change_batch.emit(items)

    def _rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self._root).as_posix()
        except ValueError:
            return str(p)

    # ── classification ─────────────────────────────────────────────────

    @staticmethod
    def classify_batch(items: List[str]) -> Dict[str, List[str]]:
        """Group changed files by strategy. Returns {strategy: [rel, ...]}."""
        out: Dict[str, List[str]] = {}
        for rel in items:
            kind = classify_change(rel)
            out.setdefault(strategy_for(kind), []).append(rel)
        return out
