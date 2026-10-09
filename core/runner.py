"""Run a script (JSON steps or a Developer Mode .py file) against one or more phones.

``ScriptRunner`` executes one run of one script. It is given either a single
``DeviceSession`` (the script runs on that phone) or a ``{role: session}``
mapping for cross-phone workflows, where each step's ``device`` role picks the
phone and all phones share one set of variables.

``run_script_on_devices`` fans a single-phone script out to several phones,
one thread each. Progress goes to a ``StatusBoard`` that a UI can poll or
subscribe to, and, when a ``RunRecorder`` is given, into the run history.
Nothing here depends on Qt.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .devices import DeviceError, DeviceSession, RunStopped
from .history import RunRecorder, StepRecord
from .schema import (
    ACTIONS, DEFAULT_TIMEOUT_SECONDS, ScriptError, describe_step, format_path, load_script, render,
    script_roles,
)

log = logging.getLogger(__name__)

LogCallback = Callable[[str, str], None]  # (serial, message)


class RunState:
    IDLE = "idle"
    CONNECTING = "connecting"
    RUNNING = "running"
    WAITING = "waiting"   # between repeated runs
    COMPLETED = "completed"
    PARTIAL = "partial"   # finished, but some steps were skipped
    FAILED = "failed"
    STOPPED = "stopped"

    FINISHED = {COMPLETED, PARTIAL, FAILED, STOPPED}


class StepFailed(Exception):
    """A step marked ``on_fail: stop`` failed: the run ends as failed."""


class StopScript(Exception):
    """A ``stop_run`` step ended the run on purpose."""


class SessionLost(Exception):
    """The Appium session or phone went away mid-run."""


_FAILED = object()  # sentinel: step failed and was skipped


# --------------------------------------------------------------------- scripts


@dataclass
class PythonScript:
    """A Developer Mode script: Python source defining ``run(device)``."""

    name: str
    source: str
    path: Path | None = None


Script = dict | PythonScript


def load_any_script(path: str | Path) -> Script:
    """Load a ``.json`` step script or a ``.py`` developer script."""
    path = Path(path)
    if path.suffix.lower() == ".py":
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScriptError(f"Could not read {path.name}: {exc}") from exc
        return PythonScript(name=path.stem, source=source, path=path)
    return load_script(path)


def script_name(script: Script) -> str:
    return script.name if isinstance(script, PythonScript) else script.get("name", "script")


def roles_of(script: Script) -> list[str]:
    """Phone roles a script needs (empty for single-phone scripts)."""
    return [] if isinstance(script, PythonScript) else script_roles(script)


def needs_role_mapping(script: Script) -> bool:
    return len(roles_of(script)) >= 2


# --------------------------------------------------------------------- status


class StatusBoard:
    """Thread-safe ``{serial: status dict}`` shared between runner threads and the UI.

    Each entry has ``state``, ``script``, ``step``, ``total_steps``, ``message``,
    ``skipped``, ``run_number`` and ``updated_at``. Listeners run on the runner's thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[str, dict], None]] = []

    def subscribe(self, listener: Callable[[str, dict], None]) -> None:
        self._listeners.append(listener)

    def update(self, serial: str, **fields: Any) -> dict:
        with self._lock:
            entry = self._entries.setdefault(serial, {"state": RunState.IDLE})
            entry.update(fields, updated_at=time.time())
            snapshot = dict(entry)
        for listener in list(self._listeners):
            try:
                listener(serial, snapshot)
            except Exception:  # a broken listener must not kill a run
                log.exception("Status listener failed")
        return snapshot

    def get(self, serial: str) -> dict:
        with self._lock:
            return dict(self._entries.get(serial, {"state": RunState.IDLE}))

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {serial: dict(entry) for serial, entry in self._entries.items()}


# --------------------------------------------------------------------- runner


@dataclass
class RunResult:
    state: str
    skipped_steps: list[str] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    message: str = ""
    report: Path | None = None


class ScriptRunner:
    """Executes one run of a script.

    A failing step is retried ``retries`` times, then either skipped (default)
    or, with ``on_fail: stop``, ends the run as failed. A failure screenshot is
    saved to the run report. The run is also aborted when a session is lost.
    """

    def __init__(self, sessions: DeviceSession | Mapping[str, DeviceSession], script: Script, *,
                 on_log: LogCallback | None = None,
                 status: StatusBoard | None = None,
                 stop_event: threading.Event | None = None,
                 variables: dict[str, Any] | None = None,
                 recorder: RunRecorder | None = None,
                 run_number: int = 1):
        self.script = script
        self.on_log = on_log
        self.status = status or StatusBoard()
        self.stop_event = stop_event or threading.Event()
        self.variables: dict[str, Any] = variables if variables is not None else {}
        self.recorder = recorder
        self.run_number = run_number

        roles = roles_of(script)
        if isinstance(sessions, DeviceSession):
            self.sessions: dict[str | None, DeviceSession] = {role: sessions for role in roles}
            self.sessions[None] = sessions
        else:
            missing = [role for role in roles if role not in sessions]
            if missing:
                raise ScriptError(f"No phone chosen for: {', '.join(missing)}")
            if not sessions:
                raise ScriptError("No phone to run on")
            self.sessions = dict(sessions)
            self.sessions[None] = sessions[roles[0]] if roles else next(iter(sessions.values()))
        self.default_role = roles[0] if roles else None
        self.serials: list[str] = list(dict.fromkeys(s.serial for s in self.sessions.values()))
        self._skipped: list[str] = []

    @property
    def serial(self) -> str:
        """The (first) phone this run uses."""
        return self.serials[0]

    # ------------------------------------------------------------ plumbing

    def log(self, message: str, session: DeviceSession | None = None) -> None:
        targets = [session.serial] if session else self.serials
        for serial in targets:
            log.info("[%s] %s", serial, message)
            if self.on_log:
                try:
                    self.on_log(serial, message)
                except Exception:
                    log.exception("Log callback failed")

    def _status(self, **fields: Any) -> None:
        for serial in self.serials:
            self.status.update(serial, **fields)

    def stop(self) -> None:
        self.stop_event.set()

    def _session_for(self, step: dict) -> DeviceSession:
        role = str(step.get("device") or "").strip() or self.default_role
        session = self.sessions.get(role)
        if session is None:
            raise ScriptError(f"No phone chosen for role {role!r}")
        return session

    def _record(self, path: str, step: dict, session: DeviceSession | None, status: str, started: float,
                attempts: int = 1, error: str = "", screenshot: str = "") -> None:
        if self.recorder:
            self.recorder.add_step(StepRecord(
                path=path, summary=describe_step(step), device=session.serial if session else "",
                status=status, started_at=started, duration=time.time() - started,
                attempts=attempts, error=error, screenshot=screenshot))

    def _failure_screenshot(self, session: DeviceSession | None, name: str) -> str:
        if not (self.recorder and session):
            return ""
        try:
            return self.recorder.save_screenshot(session.screenshot_png(), name)
        except Exception:  # the phone may be what failed
            return ""

    def _sleep(self, seconds: float) -> None:
        if self.stop_event.wait(max(0.0, float(seconds))):
            raise RunStopped("Stopped")

    # ------------------------------------------------------------ run

    def run(self) -> RunResult:
        name = script_name(self.script)
        for session in set(self.sessions.values()):
            session.should_stop = self.stop_event.is_set
        self.variables.setdefault("run_number", self.run_number)
        try:
            if isinstance(self.script, PythonScript):
                result = self._run_python(name)
            else:
                result = self._run_steps(name)
        finally:
            for session in set(self.sessions.values()):
                session.should_stop = lambda: False
        message = result.message or _final_message(result)
        result.message = message
        if self.recorder:
            result.report = self.recorder.finish(result.state, message, self.variables)
        self._status(state=result.state, message=message, skipped=len(result.skipped_steps))
        self.log(f"■ {name}: {message}")
        return result

    def _run_steps(self, name: str) -> RunResult:
        steps = self.script["steps"]
        self._skipped = []
        self._status(state=RunState.RUNNING, script=name, step=0, total_steps=len(steps),
                     message="Starting", skipped=0, run_number=self.run_number)
        roles = roles_of(self.script)
        where = (" on " + ", ".join(f"{r}={self.sessions[r].serial}" for r in roles)) if len(roles) > 1 else ""
        run_label = f" (run {self.run_number})" if self.run_number > 1 else ""
        self.log(f"▶ Running '{name}'{run_label}{where}")

        def result(state: str, error: str | None = None, message: str = "") -> RunResult:
            return RunResult(state, list(self._skipped), self.variables, error, message)

        try:
            self._run_block(steps, ())
        except RunStopped:
            return result(RunState.STOPPED)
        except StopScript as exc:
            reason = str(exc) or "Stop Script step"
            state = RunState.PARTIAL if self._skipped else RunState.COMPLETED
            return result(state, message=f"Ended by Stop Script step: {reason}")
        except StepFailed as exc:
            return result(RunState.FAILED, error=str(exc))
        except SessionLost as exc:
            return result(RunState.FAILED, error=f"Lost connection to phone: {exc}")
        except ScriptError as exc:
            return result(RunState.FAILED, error=str(exc))
        return result(RunState.PARTIAL if self._skipped else RunState.COMPLETED)

    def _run_block(self, steps: list[dict], path: tuple[int, ...]) -> None:
        for index, step in enumerate(steps, start=1):
            if self.stop_event.is_set():
                raise RunStopped("Stopped")
            here = path + (index,)
            if not path:
                self._status(step=index, message=describe_step(step))
            self._run_step(step, here)

    def _run_step(self, step: dict, path: tuple[int, ...]) -> None:
        action = step["action"]
        label = format_path(path)
        indent = "  " * len(path)

        if action == "repeat":
            times = int(step.get("times", 1))
            self.log(f"{indent}[{label}] {describe_step(step)}")
            outer = self.variables.get("loop_index")
            for iteration in range(1, times + 1):
                self.variables["loop_index"] = iteration
                self.log(f"{indent}  ↻ {iteration}/{times}")
                self._run_block(step.get("steps", []), path)
            if outer is None:
                self.variables.pop("loop_index", None)
            else:
                self.variables["loop_index"] = outer
            return

        session = self._session_for(step) if ACTIONS[action].uses_device else None
        role = f" ({step['device']})" if step.get("device") else ""
        self.log(f"{indent}[{label}]{role} {describe_step(step)}", session)
        outcome = self._attempt(step, label, session, indent)

        if action == "if_exists":
            if outcome is _FAILED:
                return
            branch = "then" if outcome else "else"
            self.log(f"{indent}  → {'found' if outcome else 'not found'}: running “{'Then' if outcome else 'Otherwise'}”",
                     session)
            self._run_block(step.get(branch, []), path)

    def _attempt(self, step: dict, label: str, session: DeviceSession | None, indent: str) -> Any:
        """Run one step with retries. Returns its value, or ``_FAILED`` if it was skipped."""
        retries = int(step.get("retries", 0) or 0)
        started = time.time()
        error = ""
        for attempt in range(1, retries + 2):
            try:
                value = self._execute(step, session)
            except (RunStopped, StopScript):
                self._record(label, step, session, "info", started, attempt)
                raise
            except Exception as exc:
                if _session_lost(exc):
                    self._record(label, step, session, "failed", started, attempt, _first_line(exc))
                    raise SessionLost(_first_line(exc)) from exc
                error = _first_line(exc)
                if attempt <= retries:
                    self.log(f"{indent}  ↺ attempt {attempt} failed ({error}); retrying…", session)
                    self._sleep(1)
                    continue
                shot = self._failure_screenshot(session, f"step_{label}_failed")
                stop = step.get("on_fail") == "stop"
                self._record(label, step, session, "failed" if stop else "skipped", started, attempt, error, shot)
                if stop:
                    self.log(f"{indent}  ✖ Step {label} failed: {error}", session)
                    raise StepFailed(f"Step {label} failed: {error}") from exc
                self._skipped.append(label)
                self._status(skipped=len(self._skipped))
                self.log(f"{indent}  ⚠ Step {label} skipped: {error}", session)
                return _FAILED
            else:
                shot = value if step["action"] == "screenshot" else ""
                self._record(label, step, session, "ok", started, attempt, screenshot=shot or "")
                return value
        return _FAILED  # unreachable; keeps type checkers calm

    def _execute(self, step: dict, session: DeviceSession | None) -> Any:
        action = step["action"]
        now = datetime.now()
        self.variables.update(date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M:%S"))
        if session is not None:
            self.variables["device"] = session.serial

        def value(name: str, default: Any = None) -> Any:
            raw = step.get(name, default)
            return render(raw, self.variables) if isinstance(raw, str) else raw

        timeout = float(step.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        locator = (step.get("locator_type"), value("locator_value"))

        if action == "wait":
            self._sleep(float(step["seconds"]))
        elif action == "set_variable":
            self.variables[step["name"]] = value("value", "")
            self.log(f"    {step['name']} = {self.variables[step['name']]!r}")
        elif action == "stop_run":
            raise StopScript(value("message", ""))
        elif action == "open_app":
            session.open_app(value("package"))
        elif action == "close_app":
            session.close_app(value("package"))
        elif action == "click":
            session.click(*locator, timeout_seconds=timeout)
        elif action == "wait_for_element":
            session.wait_for_element(*locator, timeout_seconds=timeout)
        elif action == "copy_text":
            text = session.copy_text(*locator, timeout_seconds=timeout)
            self.variables[step["save_as"]] = text
            self.log(f"    copied {text!r} → {step['save_as']}", session)
        elif action == "paste_text":
            if step.get("value_from"):
                key = step["value_from"]
                if key not in self.variables:
                    raise DeviceError(f"Nothing was copied into '{key}' yet")
                text = str(self.variables[key])
            else:
                text = value("text", "")
            session.paste_text(*locator, text, timeout_seconds=timeout)
        elif action == "scroll":
            session.scroll(step.get("direction", "down"), int(step.get("times", 1)))
        elif action == "scroll_to_text":
            session.scroll_to_text(value("text"))
        elif action == "swipe":
            session.swipe_percent(step["start_x"], step["start_y"], step["end_x"], step["end_y"],
                                  int(step.get("duration_ms", 400)))
        elif action == "tap":
            session.tap_percent(step["x"], step["y"])
        elif action == "press_key":
            session.press_key(step["key"])
        elif action == "screenshot":
            png = session.screenshot_png()
            return self.recorder.save_screenshot(png, value("name", "screenshot")) if self.recorder else ""
        elif action == "if_exists":
            return session.exists(*locator, timeout_seconds=float(step.get("timeout_seconds", 3)))
        else:
            raise DeviceError(f"Unknown action {action!r}")
        return None

    # ------------------------------------------------------------ Python

    def _run_python(self, name: str) -> RunResult:
        session = self.sessions[None]
        self._status(state=RunState.RUNNING, script=name, step=0, total_steps=0,
                     message="Running developer script", skipped=0, run_number=self.run_number)
        self.log(f"▶ Running developer script '{name}'")
        started = time.time()

        def script_print(*args: Any, sep: str = " ", **_: Any) -> None:
            self.log(sep.join(str(a) for a in args))

        namespace: dict[str, Any] = {
            "__name__": "__developer_script__",
            "print": script_print,
            "log": script_print,
            "variables": self.variables,
            "stop_requested": self.stop_event.is_set,
        }
        filename = str(self.script.path) if self.script.path else f"<{name}>"
        try:
            code = compile(self.script.source, filename, "exec")
            exec(code, namespace)  # noqa: S102 - running the user's own script is the feature
            entry = namespace.get("run")
            if not callable(entry):
                raise ScriptError("Developer scripts must define a function `run(device)`")
            entry(session)
        except RunStopped:
            return RunResult(RunState.STOPPED, variables=self.variables)
        except Exception as exc:
            self.log("  ✖ " + traceback.format_exc().rstrip().replace("\n", "\n    "))
            shot = self._failure_screenshot(session, "developer_script_failed")
            if self.recorder:
                self.recorder.add_step(StepRecord("1", f"Developer script {name}", session.serial, "failed",
                                                  started, time.time() - started, error=_first_line(exc),
                                                  screenshot=shot))
            return RunResult(RunState.FAILED, variables=self.variables, error=_first_line(exc))
        if self.recorder:
            self.recorder.add_step(StepRecord("1", f"Developer script {name}", session.serial, "ok",
                                              started, time.time() - started))
        return RunResult(RunState.COMPLETED, variables=self.variables)


def _final_message(result: RunResult) -> str:
    if result.state == RunState.COMPLETED:
        return "Completed"
    if result.state == RunState.PARTIAL:
        steps = ", ".join(result.skipped_steps)
        return f"Completed with {len(result.skipped_steps)} skipped step(s): {steps}"
    if result.state == RunState.STOPPED:
        return "Stopped"
    return f"Failed: {result.error}" if result.error else "Failed"


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0].removeprefix("Message: ")


def _session_lost(exc: BaseException) -> bool:
    """True for errors meaning the Appium session/device is gone, not just one bad step."""
    name = type(exc).__name__
    text = str(exc)
    return (name in {"InvalidSessionIdException", "MaxRetryError", "NewConnectionError", "ConnectionError"}
            or "session is either terminated or not started" in text
            or "Connection refused" in text)


# --------------------------------------------------------------------- repeated runs


def run_repeatedly(make_runner: Callable[[int], ScriptRunner], *, repeat: int = 1, delay_seconds: float = 0,
                   stop_event: threading.Event, on_wait: Callable[[int, float], None] | None = None,
                   stop_on_failure: bool = True) -> list[RunResult]:
    """Run ``repeat`` times (0 = until stopped), pausing ``delay_seconds`` between runs."""
    results: list[RunResult] = []
    run_number = 0
    while repeat == 0 or run_number < repeat:
        run_number += 1
        result = make_runner(run_number).run()
        results.append(result)
        if result.state == RunState.STOPPED or stop_event.is_set():
            break
        if result.state == RunState.FAILED and stop_on_failure:
            break
        if repeat and run_number >= repeat:
            break
        if delay_seconds > 0:
            if on_wait:
                on_wait(run_number + 1, delay_seconds)
            if stop_event.wait(delay_seconds):
                break
    return results


# --------------------------------------------------------------------- fan-out


@dataclass
class RunHandle:
    """Threads started by ``run_script_on_devices``; lets callers stop or wait for them."""

    status: StatusBoard
    threads: dict[str, threading.Thread]
    stop_events: dict[str, threading.Event]
    results: dict[str, RunResult]

    def stop(self, serial: str | None = None) -> None:
        for key, event in self.stop_events.items():
            if serial is None or key == serial:
                event.set()

    def join(self, timeout: float | None = None) -> dict[str, RunResult]:
        for thread in self.threads.values():
            thread.join(timeout)
        return self.results

    def is_running(self) -> bool:
        return any(t.is_alive() for t in self.threads.values())


def run_script_on_devices(script_path: str | Path | Script,
                          device_sessions: Mapping[str, DeviceSession] | Iterable[DeviceSession],
                          status: StatusBoard | None = None,
                          on_log: LogCallback | None = None,
                          record_history: bool = False,
                          history_dir: Path | None = None,
                          trigger: str = "cli") -> RunHandle:
    """Run one single-phone script on each device concurrently, one thread per device.

    ``script_path`` may be a path to a .json/.py file or an already-loaded
    script. Returns immediately; poll ``handle.status`` or call ``handle.join()``.
    For cross-phone workflows use ``ScriptRunner`` with a role mapping instead.
    """
    script = script_path if isinstance(script_path, (dict, PythonScript)) else load_any_script(script_path)
    if needs_role_mapping(script):
        raise ScriptError(f"'{script_name(script)}' uses several phones ({', '.join(roles_of(script))}); "
                          "run it as a workflow with a phone chosen for each role")
    sessions = list(device_sessions.values()) if isinstance(device_sessions, Mapping) else list(device_sessions)
    status = status or StatusBoard()
    handle = RunHandle(status=status, threads={}, stop_events={}, results={})

    for session in sessions:
        stop_event = threading.Event()
        recorder = (RunRecorder(script_name(script), {"device": session.serial}, trigger=trigger,
                                base_dir=history_dir) if record_history else None)
        runner = ScriptRunner(session, script, on_log=on_log, status=status, stop_event=stop_event,
                              recorder=recorder)

        def target(runner: ScriptRunner = runner) -> None:
            try:
                handle.results[runner.serial] = runner.run()
            except Exception as exc:  # never let one device's crash go unreported
                log.exception("Runner crashed on %s", runner.serial)
                status.update(runner.serial, state=RunState.FAILED, message=f"Failed: {exc}")
                handle.results[runner.serial] = RunResult(RunState.FAILED, error=str(exc))

        thread = threading.Thread(target=target, name=f"run-{session.serial}", daemon=True)
        handle.threads[session.serial] = thread
        handle.stop_events[session.serial] = stop_event
        status.update(session.serial, state=RunState.RUNNING, script=script_name(script),
                      step=0, message="Queued", skipped=0)
        thread.start()
    return handle
