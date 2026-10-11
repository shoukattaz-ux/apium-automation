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
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .devices import DEFAULT_APPIUM_URL, DeviceError, DeviceSession
from .history import RunRecorder
from .runner import (
    RunResult, RunState, Script, ScriptRunner, StatusBoard, load_any_script, needs_role_mapping, roles_of,
    run_repeatedly, script_name,
)
from .alerts import Alerter
from .errorlog import SCRIPT_FAILED, STUCK, ErrorEntry, ErrorLog
from .groups import Group
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
                 record_history: bool = True, history_dir: Path | None = None,
                 error_log: ErrorLog | None = None, alerter: Alerter | None = None):
        self.appium_url = appium_url
        self.error_log = error_log    # the Errors / Stuck log (None: not recorded)
        self.alerter = alerter        # rings when a script still fails (None: silent)
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

    def _error(self, entry: ErrorEntry) -> None:
        if self.error_log is not None:
            self.error_log.add(entry)

    def _alert(self, reason: str) -> None:
        if self.alerter is not None:
            self.alerter.alert(reason)

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

            if isinstance(script, Group):
                run.results = self._run_group(run, mapping[None], script, repeat, delay_seconds, trigger)
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
                                    stop_event=run.stop_event, recorder=recorder, run_number=run_number,
                                    on_error=self._error)

            def on_wait(next_run: int, seconds: float) -> None:
                for member in run.serials:
                    self.status.update(member, state=RunState.WAITING,
                                       message=f"Run #{next_run} starts in {seconds:g}s")
                    self._log(member, f"… waiting {seconds:g}s before run #{next_run}")

            run.results = run_repeatedly(make_runner, repeat=repeat, delay_seconds=delay_seconds,
                                         stop_event=run.stop_event, on_wait=on_wait)
            last = run.results[-1] if run.results else None
            if last and last.state == RunState.FAILED and not run.stop_event.is_set():
                self._alert(f"{name} failed on {', '.join(run.serials)}: {last.error or last.message}")
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


    # ------------------------------------------------------------------ groups

    def _run_group(self, run: _Run, serial: str, group: Group, repeat: int, delay_seconds: float,
                   trigger: str) -> list[RunResult]:
        """Run a group's scripts in order, ``repeat`` times (0 = until stopped).

        A script that fails (or gets stuck past ``max_minutes``) is retried ``group.retries``
        times on a fresh look at the phone; if it still fails it is written to the Errors log,
        the alarm rings (if the group says so) and the group goes on with the next script,
        unless the group is set to stop. A lost connection is re-opened before the next try.
        """
        results: list[RunResult] = []
        failures: list[str] = []
        halted = False  # ended by a failing script (on_script_fail == "stop"), not by the user
        pass_number = 0
        total = len(group.scripts)
        while not run.stop_event.is_set():
            pass_number += 1
            label = f" (round {pass_number})" if repeat != 1 else ""
            self._log(serial, f"▶ Group '{group.name}'{label}: {total} script(s)")
            for index, relative in enumerate(group.scripts, start=1):
                if run.stop_event.is_set():
                    break
                result, name = self._run_group_script(run, serial, group, relative, index, pass_number, trigger)
                results.append(result)
                if run.stop_event.is_set():
                    break
                if not self._group_script_ok(group, result):
                    failures.append(name)
                    reason = result.error or result.message or result.state
                    self._log(serial, f"✖ {index}/{total} '{name}' still failing after "
                                      f"{group.retries + 1} attempt(s): {reason}", logging.ERROR)
                    self._error(ErrorEntry(kind=SCRIPT_FAILED, device=serial, script=name, group=group.name,
                                           reason=reason, attempts=group.retries + 1,
                                           screenshot=self._failure_shot(serial, result),
                                           report=str(result.report or "")))
                    if group.alert:
                        self._alert(f"{group.name}: '{name}' failed on {serial}")
                    self.status.update(serial, state=RunState.WAITING, script=group.name,
                                       message=f"'{name}' failed after {group.retries + 1} attempt(s)")
                    if group.on_script_fail == "stop":
                        self._log(serial, f"■ Group '{group.name}' stopped (set to stop when a script fails)")
                        halted = True
                        run.stop_event.set()
                        break
                    self._log(serial, "→ skipping to the next script")
                if index < total:
                    # Keep the phone "busy" between scripts (Stop stays available, Start doesn't).
                    self.status.update(serial, state=RunState.WAITING, script=group.name,
                                       message=f"Group '{group.name}': next is script {index + 1}/{total}")
                    if group.pause_seconds > 0 and run.stop_event.wait(group.pause_seconds):
                        break
            if run.stop_event.is_set() or (repeat and pass_number >= repeat):
                break
            if delay_seconds > 0:
                self.status.update(serial, state=RunState.WAITING,
                                   message=f"Round {pass_number + 1} starts in {delay_seconds:g}s")
                if run.stop_event.wait(delay_seconds):
                    break
        ran = len(results)
        if run.stop_event.is_set() and not halted:
            state, message = RunState.STOPPED, f"Group '{group.name}' stopped"
        elif failures:
            state = RunState.PARTIAL if ran > len(failures) else RunState.FAILED
            message = (f"Group '{group.name}' done: {ran - len(failures)} of {ran} script run(s) OK, "
                       f"failed: {', '.join(dict.fromkeys(failures))}")
        else:
            state, message = RunState.COMPLETED, f"Group '{group.name}' done: all {ran} script run(s) OK"
        self.status.update(serial, state=state, message=message, script=group.name)
        self._log(serial, f"■ {message}", logging.WARNING if failures else logging.INFO)
        return results

    @staticmethod
    def _group_script_ok(group: Group, result: RunResult) -> bool:
        return result.state == RunState.COMPLETED or (result.state == RunState.PARTIAL and not group.retry_partial)

    def _run_group_script(self, run: _Run, serial: str, group: Group, relative: str, index: int,
                          pass_number: int, trigger: str) -> tuple[RunResult, str]:
        """One script of a group with its retries; returns its last result and its name."""
        from . import paths

        total = len(group.scripts)
        try:
            script = load_any_script(paths.configs_dir() / relative)
            if isinstance(script, Group) or needs_role_mapping(script):
                raise ScriptError("a group can only contain single-phone scripts")
        except ScriptError as exc:
            self._log(serial, f"✖ {index}/{total} {relative}: {exc}", logging.ERROR)
            return RunResult(RunState.FAILED, error=str(exc)), relative
        name = script_name(script)
        attempts = group.retries + 1
        result = RunResult(RunState.FAILED, error="not run")
        for attempt in range(1, attempts + 1):
            if run.stop_event.is_set():
                break
            tag = f" — attempt {attempt}/{attempts}" if attempt > 1 else ""
            self._log(serial, f"▷ {index}/{total} '{name}'{tag}")
            try:
                session = self.session_for(serial)
            except DeviceError as exc:
                result = RunResult(RunState.FAILED, error=f"Lost connection to phone: {exc}")
                self._log(serial, f"✖ Could not connect: {exc}", logging.ERROR)
                if run.stop_event.wait(3):
                    break
                continue
            result, stuck = self._run_with_time_limit(run, session, script, group, name, pass_number, trigger)
            if stuck:
                minutes = f"{group.max_minutes:g} min"
                result = RunResult(RunState.FAILED, result.skipped_steps, result.variables,
                                   f"Stuck: still running after {minutes} — stopped", report=result.report)
                self._log(serial, f"⏱ '{name}' ran longer than {minutes}: stopped as stuck", logging.ERROR)
                self._error(ErrorEntry(kind=STUCK, device=serial, script=name, group=group.name,
                                       reason=f"Still running after {minutes}; stopped",
                                       screenshot=self._screenshot_to_logs(session, serial),
                                       report=str(result.report or ""), attempts=attempt))
            if run.stop_event.is_set() or self._group_script_ok(group, result):
                break
            if (result.error or "").startswith("Lost connection"):
                self.drop_session(serial)  # reconnect cleanly for the next attempt
            if attempt < attempts:
                self._log(serial, f"↺ '{name}' {result.state}: {result.error or result.message}; "
                                  f"trying again ({attempt + 1}/{attempts})", logging.WARNING)
                self.status.update(serial, state=RunState.WAITING, script=group.name,
                                   message=f"Retrying '{name}' ({attempt + 1}/{attempts})")
                if run.stop_event.wait(2):
                    break
        return result, name

    def _run_with_time_limit(self, run: _Run, session: DeviceSession, script: Script, group: Group, name: str,
                             pass_number: int, trigger: str) -> tuple[RunResult, bool]:
        """Run once; a watchdog stops it (as stuck) after ``group.max_minutes``, or when the run is stopped."""
        script_stop = threading.Event()
        finished = threading.Event()
        stuck = threading.Event()
        limit = group.max_minutes * 60 if group.max_minutes > 0 else None

        def watch() -> None:
            started = time.monotonic()
            while not finished.wait(0.2):
                if run.stop_event.is_set():
                    script_stop.set()
                    return
                if limit is not None and time.monotonic() - started >= limit:
                    stuck.set()
                    script_stop.set()
                    return

        watcher = threading.Thread(target=watch, name=f"group-watch-{session.serial}", daemon=True)
        watcher.start()
        recorder = (RunRecorder(f"{group.name} › {name}", {"device": session.serial}, pass_number, trigger,
                                self.history_dir) if self.record_history else None)
        try:
            result = ScriptRunner(session, script, on_log=self._notify, status=self.status, stop_event=script_stop,
                                  recorder=recorder, run_number=pass_number, on_error=self._error,
                                  group=group.name).run()
        finally:
            finished.set()
            watcher.join(1)
        return result, stuck.is_set()

    def _failure_shot(self, serial: str, result: RunResult) -> str:
        """A screenshot for a script that failed for good (none when the connection is gone)."""
        if (result.error or "").startswith("Lost connection") or not self.has_session(serial):
            return ""
        try:
            return self._screenshot_to_logs(self.session_for(serial), serial)
        except DeviceError:
            return ""

    @staticmethod
    def _screenshot_to_logs(session: DeviceSession, serial: str) -> str:
        """What the phone showed when it got stuck, saved under logs/error-shots."""
        from . import paths

        try:
            png = session.screenshot_png()
            folder = paths.logs_dir() / "error-shots"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9]+', '_', serial)}.png"
            target.write_bytes(png)
            return str(target)
        except Exception as exc:  # the phone may be what's stuck
            log.debug("No stuck screenshot for %s: %s", serial, exc)
            return ""
