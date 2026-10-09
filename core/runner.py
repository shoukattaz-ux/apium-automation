"""Phase 2: run a script (JSON steps or a Developer Mode .py file) against devices.

``ScriptRunner`` executes one script on one ``DeviceSession``.
``run_script_on_devices`` fans a script out to several devices, one thread each,
and records per-device progress in a ``StatusBoard`` that a UI can poll or
subscribe to. Nothing here depends on Qt.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .devices import DeviceError, DeviceSession, ElementNotFound, RunStopped
from .schema import DEFAULT_TIMEOUT_SECONDS, ScriptError, describe_step, load_script

log = logging.getLogger(__name__)

LogCallback = Callable[[str, str], None]  # (serial, message)


class RunState:
    IDLE = "idle"
    CONNECTING = "connecting"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"  # finished, but some steps were skipped
    FAILED = "failed"
    STOPPED = "stopped"

    FINISHED = {COMPLETED, PARTIAL, FAILED, STOPPED}


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


# --------------------------------------------------------------------- status


class StatusBoard:
    """Thread-safe ``{serial: status dict}`` shared between runner threads and the UI.

    Each entry has ``state``, ``script``, ``step``, ``total_steps``, ``message``,
    ``skipped`` and ``updated_at``. Listeners are called on the runner's thread.
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
    skipped_steps: list[int] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ScriptRunner:
    """Executes one script on one device session.

    For JSON scripts, a step whose element is not found (or that otherwise
    fails) is logged and skipped; the run carries on with the next step. The
    run is only aborted when the device session itself is lost.
    """

    def __init__(self, session: DeviceSession, script: Script, *,
                 on_log: LogCallback | None = None,
                 status: StatusBoard | None = None,
                 stop_event: threading.Event | None = None,
                 variables: dict[str, Any] | None = None):
        self.session = session
        self.script = script
        self.on_log = on_log
        self.status = status or StatusBoard()
        self.stop_event = stop_event or threading.Event()
        self.variables: dict[str, Any] = variables if variables is not None else {}

    @property
    def serial(self) -> str:
        return self.session.serial

    def log(self, message: str) -> None:
        log.info("[%s] %s", self.serial, message)
        if self.on_log:
            try:
                self.on_log(self.serial, message)
            except Exception:
                log.exception("Log callback failed")

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> RunResult:
        name = script_name(self.script)
        self.session.should_stop = self.stop_event.is_set
        try:
            if isinstance(self.script, PythonScript):
                result = self._run_python(name)
            else:
                result = self._run_steps(name)
        finally:
            self.session.should_stop = lambda: False
        self.status.update(self.serial, state=result.state, message=_final_message(result),
                           skipped=len(result.skipped_steps))
        self.log(f"■ {name}: {_final_message(result)}")
        return result

    # ------------------------------------------------------------ JSON steps

    def _run_steps(self, name: str) -> RunResult:
        steps = self.script["steps"]
        total = len(steps)
        skipped: list[int] = []
        self.status.update(self.serial, state=RunState.RUNNING, script=name, step=0,
                           total_steps=total, message="Starting", skipped=0)
        self.log(f"▶ Running '{name}' ({total} steps)")

        for index, step in enumerate(steps, start=1):
            if self.stop_event.is_set():
                return RunResult(RunState.STOPPED, skipped, self.variables)
            summary = describe_step(step)
            self.status.update(self.serial, step=index, message=summary)
            self.log(f"  [{index}/{total}] {summary}")
            try:
                self._execute(step)
            except RunStopped:
                return RunResult(RunState.STOPPED, skipped, self.variables)
            except ElementNotFound as exc:
                skipped.append(index)
                self.log(f"  ⚠ Step {index} skipped: {exc}")
            except DeviceError as exc:
                skipped.append(index)
                self.log(f"  ⚠ Step {index} skipped: {exc}")
            except Exception as exc:
                if _session_lost(exc):
                    self.log(f"  ✖ Lost connection to device: {_first_line(exc)}")
                    return RunResult(RunState.FAILED, skipped, self.variables, error=_first_line(exc))
                skipped.append(index)
                self.log(f"  ⚠ Step {index} skipped: {_first_line(exc)}")
            self.status.update(self.serial, skipped=len(skipped))

        state = RunState.PARTIAL if skipped else RunState.COMPLETED
        return RunResult(state, skipped, self.variables)

    def _execute(self, step: Mapping[str, Any]) -> None:
        action = step["action"]
        session = self.session
        timeout = float(step.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        locator = (step.get("locator_type"), step.get("locator_value"))

        if action == "open_app":
            session.open_app(step["package"])
        elif action == "click":
            session.click(*locator, timeout_seconds=timeout)
        elif action == "scroll":
            session.scroll(step.get("direction", "down"), int(step.get("times", 1)))
        elif action == "wait":
            session.wait(float(step["seconds"]))
        elif action == "wait_for_element":
            session.wait_for_element(*locator, timeout_seconds=timeout)
        elif action == "copy_text":
            value = session.copy_text(*locator, timeout_seconds=timeout)
            self.variables[step["save_as"]] = value
            self.log(f"    copied {value!r} → {step['save_as']}")
        elif action == "paste_text":
            if step.get("value_from"):
                key = step["value_from"]
                if key not in self.variables:
                    raise DeviceError(f"Nothing was copied into '{key}' yet")
                text = str(self.variables[key])
            else:
                text = str(step.get("text", ""))
            session.paste_text(*locator, text, timeout_seconds=timeout)
        else:
            raise DeviceError(f"Unknown action {action!r}")

    # ------------------------------------------------------------ Python

    def _run_python(self, name: str) -> RunResult:
        self.status.update(self.serial, state=RunState.RUNNING, script=name, step=0,
                           total_steps=0, message="Running developer script", skipped=0)
        self.log(f"▶ Running developer script '{name}'")

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
            entry(self.session)
        except RunStopped:
            return RunResult(RunState.STOPPED, variables=self.variables)
        except Exception as exc:
            self.log("  ✖ " + traceback.format_exc().rstrip().replace("\n", "\n    "))
            return RunResult(RunState.FAILED, variables=self.variables, error=_first_line(exc))
        return RunResult(RunState.COMPLETED, variables=self.variables)


def _final_message(result: RunResult) -> str:
    if result.state == RunState.COMPLETED:
        return "Completed"
    if result.state == RunState.PARTIAL:
        steps = ", ".join(map(str, result.skipped_steps))
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
                          on_log: LogCallback | None = None) -> RunHandle:
    """Run one script on each device concurrently, one thread per device.

    ``script_path`` may be a path to a .json/.py file or an already-loaded
    script. Returns immediately; poll ``handle.status`` or call ``handle.join()``.
    """
    script = script_path if isinstance(script_path, (dict, PythonScript)) else load_any_script(script_path)
    sessions = list(device_sessions.values()) if isinstance(device_sessions, Mapping) else list(device_sessions)
    status = status or StatusBoard()
    handle = RunHandle(status=status, threads={}, stop_events={}, results={})

    for session in sessions:
        stop_event = threading.Event()
        runner = ScriptRunner(session, script, on_log=on_log, status=status, stop_event=stop_event)

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
