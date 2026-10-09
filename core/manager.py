"""Long-lived per-device run management used by the dashboard.

Each device gets its own Appium session (opened lazily on first Start, then
reused) and at most one running script at a time. Starting or stopping one
device never touches the sessions or threads of the others.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .devices import DEFAULT_APPIUM_URL, DeviceError, DeviceSession
from .runner import RunResult, RunState, Script, ScriptRunner, StatusBoard, script_name

log = logging.getLogger(__name__)


class DeviceManager:
    def __init__(self, appium_url: str = DEFAULT_APPIUM_URL,
                 session_factory: Callable[[str, str], DeviceSession] | None = None):
        self.appium_url = appium_url
        self.status = StatusBoard()
        self._session_factory = session_factory or (lambda serial, url: DeviceSession(serial, url))
        self._sessions: dict[str, DeviceSession] = {}
        self._runs: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._lock = threading.Lock()
        self._log_listeners: list[Callable[[str, str], None]] = []

    def add_log_listener(self, listener: Callable[[str, str], None]) -> None:
        self._log_listeners.append(listener)

    def _log(self, serial: str, message: str) -> None:
        for listener in list(self._log_listeners):
            try:
                listener(serial, message)
            except Exception:
                log.exception("Log listener failed")

    def is_running(self, serial: str) -> bool:
        with self._lock:
            run = self._runs.get(serial)
            return bool(run and run[0].is_alive())

    def running_serials(self) -> list[str]:
        with self._lock:
            return [serial for serial, (thread, _) in self._runs.items() if thread.is_alive()]

    def start(self, serial: str, script: Script) -> bool:
        """Start ``script`` on one device in its own thread. False if it is already busy."""
        with self._lock:
            run = self._runs.get(serial)
            if run and run[0].is_alive():
                return False
            stop_event = threading.Event()
            thread = threading.Thread(target=self._run, args=(serial, script, stop_event),
                                      name=f"device-{serial}", daemon=True)
            self._runs[serial] = (thread, stop_event)
        self.status.update(serial, state=RunState.CONNECTING, script=script_name(script),
                           step=0, total_steps=0, message="Preparing", skipped=0)
        thread.start()
        return True

    def stop(self, serial: str) -> None:
        with self._lock:
            run = self._runs.get(serial)
        if run and run[0].is_alive():
            run[1].set()
            self._log(serial, "Stop requested…")

    def stop_all(self) -> None:
        for serial in self.running_serials():
            self.stop(serial)

    def device_removed(self, serial: str) -> None:
        """A phone was unplugged: stop its run and drop its session."""
        self.stop(serial)
        with self._lock:
            session = self._sessions.pop(serial, None)
        if session:
            threading.Thread(target=session.close, daemon=True).start()

    def session_for(self, serial: str) -> DeviceSession:
        """Connected session for ``serial``, opening one if needed (blocking)."""
        with self._lock:
            session = self._sessions.get(serial)
        if session and session.is_connected:
            return session
        session = self._session_factory(serial, self.appium_url)
        with self._lock:
            self._sessions[serial] = session
        return session

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop_all()
        with self._lock:
            threads = [thread for thread, _ in self._runs.values()]
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for thread in threads:
            thread.join(timeout / max(1, len(threads)))
        for session in sessions:
            session.close()

    def _run(self, serial: str, script: Script, stop_event: threading.Event) -> None:
        name = script_name(script)
        try:
            self._log(serial, f"Connecting to {serial}…")
            try:
                session = self.session_for(serial)
            except DeviceError as exc:
                self._log(serial, f"✖ {exc}")
                self.status.update(serial, state=RunState.FAILED, message=str(exc))
                return
            if stop_event.is_set():
                self.status.update(serial, state=RunState.STOPPED, message="Stopped")
                return
            runner = ScriptRunner(session, script, on_log=self._log, status=self.status, stop_event=stop_event)
            result: RunResult = runner.run()
            # A failed JSON run means the session was lost (bad steps are only skipped),
            # so drop it and let the next Start reconnect. A failed developer script
            # is usually a bug in the script, and the session is still fine.
            if result.state == RunState.FAILED and isinstance(script, dict):
                with self._lock:
                    lost = self._sessions.pop(serial, None)
                if lost:
                    lost.close()
        except Exception as exc:
            log.exception("Run of %s on %s crashed", name, serial)
            self._log(serial, f"✖ Unexpected error: {exc}")
            self.status.update(serial, state=RunState.FAILED, message=f"Failed: {exc}")
