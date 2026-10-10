"""Long-lived run management used by the dashboard and the scheduler.

Each phone gets its own Appium session (opened lazily on first use, then
reused). A phone runs at most one thing at a time: either a single-phone
script, or its part in a cross-phone workflow, which reserves every phone it
uses for the whole run. Starting or stopping one run never touches the
sessions or threads of the others.
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .devices import DEFAULT_APPIUM_URL, DeviceError, DeviceSession
from .history import RunRecorder
from .runner import (
    RunResult, RunState, Script, ScriptRunner, StatusBoard, needs_role_mapping, roles_of, run_repeatedly,
    script_name,
)
from .schema import ScriptError

log = logging.getLogger(__name__)

FinishedListener = Callable[[list[str], list[RunResult]], None]


@dataclass
class _Run:
    run_id: int
    serials: list[str]
    stop_event: threading.Event
    thread: threading.Thread | None = None
    results: list[RunResult] = field(default_factory=list)


class DeviceManager:
    def __init__(self, appium_url: str = DEFAULT_APPIUM_URL,
                 session_factory: Callable[[str, str], DeviceSession] | None = None,
                 record_history: bool = True, history_dir: Path | None = None):
        self.appium_url = appium_url
        self.status = StatusBoard()
        self.record_history = record_history
        self.history_dir = history_dir
        self._session_factory = session_factory or (lambda serial, url: DeviceSession(serial, url))
        self._sessions: dict[str, DeviceSession] = {}
        self._runs: dict[int, _Run] = {}
        self._busy: dict[str, int] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._log_listeners: list[Callable[[str, str], None]] = []
        self._finished_listeners: list[FinishedListener] = []

    # ------------------------------------------------------------------ listeners

    def add_log_listener(self, listener: Callable[[str, str], None]) -> None:
        self._log_listeners.append(listener)

    def add_finished_listener(self, listener: FinishedListener) -> None:
        """Called (on the run's thread) after a run — including all its repeats — ends."""
        self._finished_listeners.append(listener)

    def _log(self, serial: str, message: str, level: int = logging.INFO) -> None:
        """Write to the log file and show in the UI log panel."""
        log.log(level, "[%s] %s", serial, message)
        self._notify(serial, message)

    def _notify(self, serial: str, message: str) -> None:
        """Show in the UI log panel only (the runner logs to file itself)."""
        for listener in list(self._log_listeners):
            try:
                listener(serial, message)
            except Exception:
                log.exception("Log listener failed")

    # ------------------------------------------------------------------ queries

    def is_running(self, serial: str) -> bool:
        with self._lock:
            return serial in self._busy

    def running_serials(self) -> list[str]:
        with self._lock:
            return list(self._busy)

    def partners(self, serial: str) -> list[str]:
        """All phones in the same run as ``serial`` (itself included)."""
        with self._lock:
            run = self._runs.get(self._busy.get(serial, -1))
            return list(run.serials) if run else []

    # ------------------------------------------------------------------ starting

    def start(self, serial: str, script: Script, *, repeat: int = 1, delay_seconds: float = 0,
              trigger: str = "manual") -> bool:
        """Start a single-phone script on one phone. False if the phone is busy."""
        if needs_role_mapping(script):
            raise ScriptError(f"'{script_name(script)}' uses several phones; choose a phone for each of "
                              f"{', '.join(roles_of(script))}")
        return self._start({None: serial}, script, repeat, delay_seconds, trigger)

    def start_workflow(self, mapping: dict[str, str], script: Script, *, repeat: int = 1,
                       delay_seconds: float = 0, trigger: str = "manual") -> bool:
        """Start a cross-phone workflow; ``mapping`` is ``{role: serial}``. False if any phone is busy."""
        roles = roles_of(script)
        missing = [role for role in roles if not mapping.get(role)]
        if missing:
            raise ScriptError(f"Choose a phone for: {', '.join(missing)}")
        # One phone may play several roles: it gets one session, and the steps still run in order.
        return self._start({role: mapping[role] for role in roles}, script, repeat, delay_seconds, trigger)

    def _start(self, mapping: dict[str | None, str], script: Script, repeat: int, delay_seconds: float,
               trigger: str) -> bool:
        serials = list(dict.fromkeys(mapping.values()))
        with self._lock:
            if any(serial in self._busy for serial in serials):
                return False
            run = _Run(next(self._ids), serials, threading.Event())
            for serial in serials:
                self._busy[serial] = run.run_id
            self._runs[run.run_id] = run
            run.thread = threading.Thread(target=self._run, args=(run, mapping, script, repeat, delay_seconds, trigger),
                                          name=f"run-{run.run_id}", daemon=True)
        for serial in serials:
            self.status.update(serial, state=RunState.CONNECTING, script=script_name(script), step=0,
                               total_steps=0, message="Preparing", skipped=0, partners=serials)
        run.thread.start()
        return True

    # ------------------------------------------------------------------ stopping

    def stop(self, serial: str) -> None:
        """Stop whatever run ``serial`` is part of (a workflow stops on all its phones)."""
        with self._lock:
            run = self._runs.get(self._busy.get(serial, -1))
        if run and not run.stop_event.is_set():
            run.stop_event.set()
            for member in run.serials:
                self._log(member, "Stop requested…")

    def stop_all(self) -> None:
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            run.stop_event.set()

    def device_removed(self, serial: str) -> None:
        """A phone was unplugged: stop its run and drop its session."""
        self.stop(serial)
        with self._lock:
            session = self._sessions.pop(serial, None)
        if session:
            threading.Thread(target=session.close, daemon=True).start()

    # ------------------------------------------------------------------ sessions

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

    def has_session(self, serial: str) -> bool:
        """True if ``serial`` already has an open session (so using it won't need to connect)."""
        with self._lock:
            session = self._sessions.get(serial)
        return bool(session and session.is_connected)

    def drop_session(self, serial: str) -> None:
        with self._lock:
            session = self._sessions.pop(serial, None)
        if session:
            session.close()

    def drop_all_sessions(self) -> None:
        """Forget every session (e.g. the Appium server stopped): the next use opens a fresh one.

        Closing a session whose server is gone can take a few seconds, so that happens
        on a background thread.
        """
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        if sessions:
            log.info("Dropping %d Appium session(s)", len(sessions))
            threading.Thread(target=lambda: [s.close() for s in sessions], name="drop-sessions",
                             daemon=True).start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop_all()
        with self._lock:
            threads = [run.thread for run in self._runs.values() if run.thread]
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for thread in threads:
            thread.join(timeout / max(1, len(threads)))
        for session in sessions:
            session.close()

    # ------------------------------------------------------------------ worker

    def _run(self, run: _Run, mapping: dict[str | None, str], script: Script, repeat: int,
             delay_seconds: float, trigger: str) -> None:
        name = script_name(script)
        try:
            sessions: dict[str, DeviceSession] = {}
            for serial in run.serials:
                self._log(serial, f"Connecting to {serial}…")
                try:
                    sessions[serial] = self.session_for(serial)
                except DeviceError as exc:
                    for member in run.serials:
                        self._log(member, f"✖ {exc}", logging.ERROR)
                        self.status.update(member, state=RunState.FAILED, message=str(exc))
                    return
                if run.stop_event.is_set():
                    for member in run.serials:
                        self.status.update(member, state=RunState.STOPPED, message="Stopped")
                    return

            if None in mapping:
                target: DeviceSession | dict[str, DeviceSession] = sessions[mapping[None]]
                devices = {"device": mapping[None]}
            else:
                target = {role: sessions[serial] for role, serial in mapping.items()}
                devices = {role: serial for role, serial in mapping.items()}

            def make_runner(run_number: int) -> ScriptRunner:
                recorder = (RunRecorder(name, devices, run_number, trigger, self.history_dir)
                            if self.record_history else None)
                return ScriptRunner(target, script, on_log=self._notify, status=self.status,
                                    stop_event=run.stop_event, recorder=recorder, run_number=run_number)

            def on_wait(next_run: int, seconds: float) -> None:
                for member in run.serials:
                    self.status.update(member, state=RunState.WAITING,
                                       message=f"Run #{next_run} starts in {seconds:g}s")
                    self._log(member, f"… waiting {seconds:g}s before run #{next_run}")

            run.results = run_repeatedly(make_runner, repeat=repeat, delay_seconds=delay_seconds,
                                         stop_event=run.stop_event, on_wait=on_wait)
            last = run.results[-1] if run.results else None
            if (last and last.state == RunState.FAILED and isinstance(script, dict)
                    and (last.error or "").startswith("Lost connection")):
                # Drop the broken sessions so the next Start reconnects cleanly.
                for serial in run.serials:
                    self.drop_session(serial)
            if len(run.results) > 1:
                summary = ", ".join(f"#{i}: {r.state}" for i, r in enumerate(run.results, start=1))
                for member in run.serials:
                    self._log(member, f"■ {len(run.results)} runs finished ({summary})")
        except Exception as exc:
            log.exception("Run of %s crashed", name)
            for member in run.serials:
                self._log(member, f"✖ Unexpected error: {exc}", logging.ERROR)
                self.status.update(member, state=RunState.FAILED, message=f"Failed: {exc}")
        finally:
            with self._lock:
                for serial in run.serials:
                    if self._busy.get(serial) == run.run_id:
                        del self._busy[serial]
                self._runs.pop(run.run_id, None)
            for listener in list(self._finished_listeners):
                try:
                    listener(run.serials, run.results)
                except Exception:
                    log.exception("Finished listener failed")
