"""Preview supervisor — keeps the real Spatial UI running on the TV.

Responsibilities
  * spawn / monitor the preview child process
  * watch source files and classify changes (style / resource / layout /
    logic / config / ignore)
  * apply the right strategy:
      style    -> hot style re-apply in the running process
      resource -> resource reload in the running process
      layout   -> safe process restart (UI structure changed)
      logic    -> safe process restart (core Python changed)
      config   -> log + notify (does not auto-restart)
      ignore   -> do nothing
  * debounce bursts of saves (fast consecutive writes collapse into one
    restart)
  * guard against infinite restart loops: if the same file-signature
    keeps failing, stop retrying until the signature changes
  * last-good-frame policy: on a failed startup, keep the previously
    running process alive (or restart it) so the TV never goes dark
    with an error splash

The supervisor itself runs headless (no Qt). It tracks child liveness
via subprocess handles and the status.json the child writes.

Usage:
  python -m tools.spatial_preview.supervisor [options]
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional

from tools.spatial_preview import preview_config as cfg
from tools.spatial_preview.file_watcher import (
    Debouncer,
    PreviewFileWatcher,
    classify_change,
    file_signature,
    strategy_for,
)

PROJECT_ROOT = cfg.PROJECT_ROOT
PY = Path(os.environ.get("ARONA_PYTHON", str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")))

READY_TIMEOUT_S = 120.0   # 3D model loading can take 30-90s on low-spec hardware
STARTUP_POLL_S = 0.5
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_S = 60.0


class _Logger:
    def __init__(self):
        cfg.ensure_dirs()
        self._path = cfg.PREVIEW_LOG_FILE

    def info(self, msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [sup] {msg}"
        print(line, flush=True)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    warn = error = info


class Supervisor:
    """Manages the preview child and the hot-reload loop."""

    def __init__(self, display: Optional[str] = None,
                 fullscreen: bool = True,
                 perf_mode: str = "medium",
                 enable_3d: bool = True,
                 no_reload: bool = False):
        self.display = display
        self.fullscreen = fullscreen
        self.perf_mode = perf_mode
        self.enable_3d = enable_3d
        self.no_reload = no_reload

        self.log = _Logger()
        self._child: Optional[subprocess.Popen] = None
        self._child_pid: Optional[int] = None
        self._seq = 0
        self._stop = threading.Event()

        self._consecutive_failures = 0
        self._last_failed_signature: Optional[str] = None
        self._cooldown_until = 0.0

        self._watcher: Optional[PreviewFileWatcher] = None
        self._debounce_ms = 1000
        self._ready_timeout = READY_TIMEOUT_S
        # "inplace"  -> re-apply QSS in the running process (fast, no flicker)
        # "restart"  -> full process restart (needed if inline styles built from
        #               COLOR_* constants must be rebuilt)
        self.style_mode = "inplace"

    # ── single-instance lock ────────────────────────────────────────

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            # On Windows os.kill(pid, 0) still works as an existence probe
            # (it raises OSError if the pid does not exist).
            import ctypes
            kernel32 = ctypes.windll.kernel32
            STILL_RUNNING = 259
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if handle == 0:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False

    def _acquire_lock(self) -> bool:
        """Prevent two supervisors from racing. Returns False if a live
        supervisor already holds the lock."""
        import json as _json
        cfg.ensure_dirs()
        lock = cfg.SUP_PID_FILE
        if lock.exists():
            try:
                data = _json.loads(lock.read_text(encoding="utf-8"))
                old_pid = int(data.get("pid", -1))
                if old_pid > 0 and self._pid_alive(old_pid):
                    return False
            except Exception:
                pass
        # stale or absent -> take it
        try:
            tmp = lock.with_suffix(".tmp")
            tmp.write_text(_json.dumps({"pid": os.getpid()}), encoding="utf-8")
            tmp.replace(lock)
            return True
        except Exception:
            return False

    @staticmethod
    def _release_lock() -> None:
        try:
            cfg.SUP_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # ── child process ───────────────────────────────────────────────

    def _env(self) -> dict:
        env = dict(os.environ)
        env["ARONA_PREVIEW"] = "1"
        if self.display:
            env["ARONA_PREVIEW_DISPLAY"] = self.display
        else:
            env.pop("ARONA_PREVIEW_DISPLAY", None)
        env["ARONA_PREVIEW_FULLSCREEN"] = "1" if self.fullscreen else "0"
        env["ARONA_PREVIEW_PERF"] = self.perf_mode
        env["ARONA_PREVIEW_3D"] = "1" if self.enable_3d else "0"
        return env

    @property
    def _child_errlog_path(self) -> Path:
        return cfg.PREVIEW_LOG_DIR / "child_stderr.log"

    def _tail_child_error(self, max_lines: int = 25) -> str:
        """Return the tail of the child's captured stderr (for error reporting)."""
        p = self._child_errlog_path
        if not p.exists():
            return "(no child output captured)"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(child output unreadable)"
        tail = [ln for ln in lines if ln.strip()][-max_lines:]
        return "\n".join(tail) if tail else "(child produced no stderr output)"

    def _start_child(self, reason: str = "initial") -> bool:
        cfg.ensure_dirs()
        cfg.write_status("starting", reason=reason)

        args = [str(PY), "-m", "tools.spatial_preview.preview_runtime"]
        self.log.info(f"spawning child ({reason}): {' '.join(args)}")
        try:
            # Capture child stdout/stderr into a log file so that import /
            # OpenGL / window-creation failures have a concrete cause in the
            # logs instead of a bare exit code.
            try:
                errlog = open(self._child_errlog_path, "a", encoding="utf-8")
            except OSError:
                errlog = subprocess.DEVNULL
            errlog.write(f"\n===== child start ({reason}) "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            errlog.flush()
            self._child = subprocess.Popen(
                args,
                cwd=str(PROJECT_ROOT),
                env=self._env(),
                stdout=errlog,
                stderr=subprocess.STDOUT,
                creationflags=(0x08000000 if os.name == "nt" else 0),  # DETACHED_PROCESS
            )
            self._child_pid = self._child.pid
            self.log.info(f"child pid={self._child_pid}")
            return True
        except Exception as exc:
            self.log.error(f"failed to spawn child: {exc}")
            return False

    def _wait_ready(self, timeout: float = READY_TIMEOUT_S) -> bool:
        """Wait until the child reports state=running in status.json."""
        t0 = time.time()
        last_state = None
        while time.time() - t0 < timeout:
            if self._child is not None and self._child.poll() is not None:
                self.log.error(f"child exited early (code={self._child.returncode})")
                cfg.write_status("startup_failed",
                                 detail=f"child exited code={self._child.returncode}")
                return False
            st = cfg.read_status() or {}
            state = st.get("state")
            if state != last_state:
                self.log.info(f"child state: {state} {st.get('display_name','')}")
                last_state = state
            if state == "running":
                return True
            time.sleep(STARTUP_POLL_S)
        self.log.error(f"child did not reach running within {timeout}s")
        cfg.write_status("startup_timeout")
        return False

    @staticmethod
    def _kill_tree(pid: int) -> None:
        """Kill a process AND its descendants.

        Windows venvs use a redirector stub (.venv/Scripts/python.exe) that
        launches the real interpreter as a child process, so terminating only
        the stub can orphan the actual preview window.
        """
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    def _stop_child(self, grace_s: float = 8.0) -> None:
        if self._child is None:
            return
        pid = self._child_pid
        try:
            self._child.terminate()
            self._child.wait(timeout=grace_s)
            self.log.info(f"child pid={pid} terminated cleanly")
        except subprocess.TimeoutExpired:
            self.log.warn(f"child pid={pid} did not exit; killing tree")
            self._child.kill()
            try:
                self._child.wait(timeout=3)
            except Exception:
                pass
        except Exception as exc:
            self.log.warn(f"terminate error: {exc}")
        # always clean up descendants (venv stub -> real interpreter)
        if pid:
            self._kill_tree(pid)
        self._child = None
        self._child_pid = None

    # ── restart policy ──────────────────────────────────────────────

    def _restart_child(self, reason: str, keep_old: bool = True) -> bool:
        """Safely restart the preview process.

        keep_old=True: start the NEW process and only kill the old one once
        the new one is confirmed running (last-good-frame policy — the TV
        never goes dark).  If the new one fails, the old one keeps showing
        the last good UI.
        """
        old = self._child
        old_pid = self._child_pid
        self.log.info(f"restart requested ({reason}); old pid={old_pid}")

        # 1. spawn new child alongside the old one
        if not self._start_child(reason=f"restart:{reason}"):
            self.log.error("new child failed to spawn; keeping old process")
            self._child = old
            self._child_pid = old_pid
            return False

        # 2. wait for the new child to be ready
        if self._wait_ready(self._ready_timeout):
            # 3. only now kill the old child
            if old is not None:
                self.log.info("new child ready; stopping old child")
                self._terminate_pid(old, old_pid)
            self._consecutive_failures = 0
            self._last_failed_signature = None
            self._cooldown_until = 0.0
            return True

        # new child failed -> kill ONLY the failed new child and restore the
        # handle to the still-running previous process (last-good-frame).
        failed = self._child
        failed_pid = self._child_pid
        self._child = old
        self._child_pid = old_pid

        self.log.error("new child failed to start; keeping previous process "
                       f"(pid={old_pid})")
        if failed is not None:
            try:
                self._terminate_pid(failed, failed_pid)
            except Exception:
                pass
        # surface the concrete failure cause from the captured child output
        err = self._tail_child_error()
        if err and err != "(no child output captured)":
            self.log.error("child error output (tail):\n" + err)
            cfg.write_status("restart_failed", reason=reason, detail=err[-2000:])
        else:
            cfg.write_status("restart_failed", reason=reason)

        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._cooldown_until = time.time() + COOLDOWN_S
            self.log.error(f"too many failures; cooling down {COOLDOWN_S}s")
        return False

    def _terminate_pid(self, proc: subprocess.Popen, pid: int) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception as exc:
            self.log.warn(f"terminate pid={pid}: {exc}")
        if pid:
            self._kill_tree(pid)

    # ── hot reload strategies ───────────────────────────────────────

    def _send_command(self, cmd: str, reason: str = "") -> None:
        self._seq += 1
        cfg.write_command(cmd, seq=self._seq, reason=reason)

    def on_change_batch(self, items: List[str]) -> None:
        """Called by the file watcher with a debounced list of changed paths."""
        if self.no_reload:
            return
        groups: Dict[str, List[str]] = {}
        for rel in items:
            kind = classify_change(rel)
            groups.setdefault(strategy_for(kind), []).append(rel)

        self.log.info(f"change batch: " +
                      "; ".join(f"{k}={v}" for k, v in groups.items()))

        # highest-priority strategy wins
        priority = [k for k in ("restart_process", "rebuild_ui",
                                 "reload_resources", "hot_reload_style",
                                 "check_and_notify", "ignore")
                    if k in groups]

        for strat in priority:
            if strat in ("restart_process", "rebuild_ui"):
                files = groups[strat]
                # infinite-loop guard
                sig = file_signature([PROJECT_ROOT / f for f in files
                                      if (PROJECT_ROOT / f).exists()])
                if sig == self._last_failed_signature:
                    self.log.warn("same files keep failing; not retrying "
                                  "until they change again")
                    return
                if time.time() < self._cooldown_until:
                    self.log.info("in cooldown window; deferring restart")
                    return
                if self._restart_child(reason=strat):
                    self._last_failed_signature = None
                else:
                    self._last_failed_signature = sig
                return
            if strat == "reload_resources":
                self._send_command("reload_resources",
                                   reason=";".join(groups[strat]))
                return
            if strat == "hot_reload_style":
                if self.style_mode == "restart":
                    files = groups[strat]
                    sig = file_signature([PROJECT_ROOT / f for f in files
                                          if (PROJECT_ROOT / f).exists()])
                    if sig == self._last_failed_signature:
                        self.log.warn("same style files keep failing; "
                                      "not retrying until they change again")
                        return
                    if time.time() < self._cooldown_until:
                        self.log.info("in cooldown window; deferring restart")
                        return
                    if self._restart_child(reason="style(restart)"):
                        self._last_failed_signature = None
                    else:
                        self._last_failed_signature = sig
                    return
                self._send_command("reload_style",
                                   reason=";".join(groups[strat]))
                return
            if strat == "check_and_notify":
                self.log.warn("config/dependency changed: "
                              + "; ".join(groups[strat])
                              + " — may require a full restart; "
                                "run supervisor restart manually if needed")
                return

    # ── main loop ───────────────────────────────────────────────────

    def run(self) -> int:
        self.log.info("supervisor starting")
        if not self._acquire_lock():
            self.log.error("another supervisor is already running; exiting")
            return 3
        self.log.info(f"display={self.display or 'auto'} "
                      f"fullscreen={self.fullscreen} perf={self.perf_mode} "
                      f"3d={self.enable_3d}")

        # ── initial start with bounded retries ──────────────────────
        # On a memory-constrained laptop Qt + PMX model loading can take
        # well over a minute, so a single timeout is not a fair failure.
        started = False
        try:
            for attempt in range(1, MAX_CONSECUTIVE_FAILURES + 1):
                self.log.info(f"initial start attempt {attempt}/"
                              f"{MAX_CONSECUTIVE_FAILURES}")
                ok = self._start_child(reason=f"initial#{attempt}")
                if ok and self._wait_ready(self._ready_timeout):
                    started = True
                    self._consecutive_failures = 0
                    break
                self._stop_child()
                self._consecutive_failures = attempt
                if attempt < MAX_CONSECUTIVE_FAILURES:
                    backoff = 5.0 * attempt
                    self.log.warn(f"attempt {attempt} failed; "
                                  f"retrying in {backoff:.0f}s")
                    time.sleep(backoff)
        finally:
            if not started:
                self._stop_child()
                cfg.write_status("startup_failed",
                                 detail="preview never reached running")
                self._release_lock()
                self.log.error("preview could not start after "
                               f"{MAX_CONSECUTIVE_FAILURES} attempts; "
                               f"see {cfg.PREVIEW_LOG_FILE}")
                return 2

        # Watch files (needs a Qt event loop; the supervisor uses a plain
        # QCoreApplication via PySide6 — no widgets, no window)
        if not self.no_reload:
            from PySide6.QtCore import QCoreApplication, QEventLoop
            from PySide6.QtCore import QTimer

            app = QCoreApplication.instance() or QCoreApplication([])
            watcher = PreviewFileWatcher(
                PROJECT_ROOT, cfg.WATCH_GLOBS, quiet_ms=self._debounce_ms
            )
            watcher.change_batch.connect(self.on_change_batch)
            watcher.start()
            self._watcher = watcher
            self.log.info("file watcher started")

            loop_timer = QTimer()
            loop_timer.setInterval(2000)

            def _watchdog():
                # If the child died unexpectedly, try to bring it back.
                if self._child is None or self._child.poll() is None:
                    return
                code = self._child.returncode
                self.log.warn(f"child died unexpectedly (code={code})")
                # remember the old (dead) handle then clear it
                self._child = None
                self._child_pid = None
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.log.error("max consecutive failures reached; "
                                   "stop watching; manual restart needed")
                    loop_timer.stop()
                    return
                if not self._restart_child(reason="auto-restart-after-crash"):
                    self.log.error("auto-restart failed; "
                                   f"previous failure count="
                                   f"{self._consecutive_failures}")

            loop_timer.timeout.connect(_watchdog)
            loop_timer.start()

            # run until stopped
            loop = QEventLoop()
            stop_timer = QTimer()
            stop_timer.setInterval(100)

            def _stop_check():
                if self._stop.is_set():
                    loop.quit()

            stop_timer.timeout.connect(_stop_check)
            stop_timer.start()

            self.log.info("supervisor loop running; Ctrl+C to stop")
            try:
                loop.exec()
            except KeyboardInterrupt:
                pass
            finally:
                stop_timer.stop()
                loop_timer.stop()
                watcher.stop()

        # Clean shutdown (single place that always releases the lock
        # and kills the child process tree)
        self.log.info("shutting down preview")
        self.cleanup()
        cfg.write_status("stopped", reason="supervisor-exit")
        self.log.info("supervisor stopped")
        return 0

    def stop(self) -> None:
        self._stop.set()

    def cleanup(self) -> None:
        """Best-effort cleanup used from exception/exit paths."""
        try:
            self._send_command("shutdown", reason="supervisor-cleanup")
            time.sleep(0.2)
        except Exception:
            pass
        try:
            self._stop_child()
        except Exception:
            pass
        self._release_lock()


# ── CLI ──────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="AronaClient Spatial UI TV preview supervisor")
    p.add_argument("--display", default=None,
                   help="display index or name substring "
                        "(default: auto = largest non-primary screen)")
    p.add_argument("--windowed", action="store_true",
                   help="maximized instead of fullscreen")
    p.add_argument("--perf", default="medium",
                   choices=["low", "medium", "high"],
                   help="performance mode (default medium)")
    p.add_argument("--no-3d", action="store_true",
                   help="disable the 3D model runtime")
    p.add_argument("--no-reload", action="store_true",
                   help="start once without watching for file changes")
    p.add_argument("--stop", action="store_true",
                   help="stop a running supervisor/preview and exit")
    p.add_argument("--style-mode", default="inplace",
                   choices=["inplace", "restart"],
                   help="style/theme edits: re-apply QSS in place (default) or "
                        "do a full process restart")
    p.add_argument("--startup-timeout", type=float, default=READY_TIMEOUT_S,
                   help=f"seconds to wait for the preview window to appear "
                        f"(default {READY_TIMEOUT_S:.0f}); raise on slow machines")
    args = p.parse_args(argv)

    sup = Supervisor(
        display=args.display,
        fullscreen=not args.windowed,
        perf_mode=args.perf,
        enable_3d=not args.no_3d,
        no_reload=args.no_reload,
    )

    sup._ready_timeout = args.startup_timeout
    sup.style_mode = args.style_mode

    if args.stop:
        sup.log.info("stopping running preview")
        try:
            cfg.write_command("shutdown", seq=99999, reason="manual-stop")
        except Exception:
            pass
        time.sleep(1.2)
        # kill any surviving child/supervisor process trees
        st = cfg.read_status() or {}
        child_pid = st.get("pid")
        if isinstance(child_pid, int) and child_pid > 0:
            Supervisor._kill_tree(child_pid)
        if cfg.SUP_PID_FILE.exists():
            try:
                import json as _json
                sup_pid = int(_json.loads(
                    cfg.SUP_PID_FILE.read_text(encoding="utf-8")).get("pid", -1))
                if sup_pid > 0:
                    Supervisor._kill_tree(sup_pid)
            except Exception:
                pass
            Supervisor._release_lock()
        cfg.write_status("stopped", reason="manual-stop")
        sup.log.info("preview stopped")
        return 0

    def _sigint(*_):
        sup.log.info("SIGINT — stopping")
        sup.stop()

    signal.signal(signal.SIGINT, _sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sigint)

    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())
