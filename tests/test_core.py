import json
import threading

import pytest

from core.devices import DeviceSession, ElementNotFound, RunStopped, parse_adb_devices
from core.library import list_scripts
from core.runner import PythonScript, RunState, ScriptRunner, StatusBoard, run_script_on_devices
from core.schema import ScriptError, load_script, save_script, validate_script, validate_step


class FakeSession(DeviceSession):
    """DeviceSession whose actions are recorded instead of sent to a phone."""

    def __init__(self, serial, screen=None):
        super().__init__(serial, connect=False)
        self.driver = object()  # looks connected
        self.screen = screen or {}
        self.calls = []

    def _element(self, locator_type, locator_value):
        self._require_driver()
        key = f"{locator_type}={locator_value}"
        if key not in self.screen:
            raise ElementNotFound(f"Element {key} not found")
        return key

    def open_app(self, package):
        self._require_driver()
        self.calls.append(("open_app", package))

    def click(self, locator_type, locator_value, timeout_seconds=15):
        self.calls.append(("click", self._element(locator_type, locator_value)))

    def wait_for_element(self, locator_type, locator_value, timeout_seconds=15):
        self._element(locator_type, locator_value)

    def copy_text(self, locator_type, locator_value, timeout_seconds=15):
        return self.screen[self._element(locator_type, locator_value)]

    def paste_text(self, locator_type, locator_value, text, timeout_seconds=15):
        key = self._element(locator_type, locator_value)
        self.screen[key] = text
        self.calls.append(("paste", key, text))

    def scroll(self, direction="down", times=1):
        self._require_driver()
        self.calls.append(("scroll", direction, times))


SCRIPT = {
    "name": "Copy task",
    "steps": [
        {"action": "open_app", "package": "com.a"},
        {"action": "copy_text", "locator_type": "id", "locator_value": "a:id/title", "save_as": "copied"},
        {"action": "click", "locator_type": "id", "locator_value": "a:id/missing", "timeout_seconds": 0},
        {"action": "scroll", "direction": "down", "times": 2},
        {"action": "paste_text", "locator_type": "id", "locator_value": "b:id/input", "value_from": "copied"},
        {"action": "wait", "seconds": 0},
    ],
}


def test_parse_adb_devices():
    output = "List of devices attached\nABC123\tdevice\nXYZ\tunauthorized\n\n"
    assert parse_adb_devices(output) == (["ABC123"], {"XYZ": "unauthorized"})


def test_validation_reports_missing_fields():
    assert validate_step({"action": "click", "locator_type": "id"}) == {"locator_value": "Locator value is required"}
    assert "value_from" in validate_step({"action": "paste_text", "locator_type": "id", "locator_value": "x"})
    assert validate_step({"action": "scroll", "direction": "sideways", "times": 1})
    assert validate_script(SCRIPT) == []
    assert validate_script({"name": "", "steps": []})


def test_runner_skips_missing_element_and_continues():
    session = FakeSession("dev1", {"id=a:id/title": "hello", "id=b:id/input": ""})
    logs = []
    result = ScriptRunner(session, SCRIPT, on_log=lambda s, m: logs.append(m)).run()
    assert result.state == RunState.PARTIAL
    assert result.skipped_steps == [3]
    assert session.screen["id=b:id/input"] == "hello"
    assert ("scroll", "down", 2) in session.calls
    assert any("skipped" in line for line in logs)


def test_runs_on_several_devices_independently():
    sessions = {s: FakeSession(s, {"id=a:id/title": s, "id=b:id/input": ""}) for s in ("d1", "d2", "d3")}
    board = StatusBoard()
    handle = run_script_on_devices(SCRIPT, sessions, status=board)
    results = handle.join(timeout=10)
    assert set(results) == {"d1", "d2", "d3"}
    for serial, session in sessions.items():
        assert session.screen["id=b:id/input"] == serial  # no cross-talk between devices
        assert board.get(serial)["state"] == RunState.PARTIAL


def test_stop_interrupts_run():
    session = FakeSession("dev1")
    stop = threading.Event()
    stop.set()
    result = ScriptRunner(session, SCRIPT, stop_event=stop).run()
    assert result.state == RunState.STOPPED
    assert session.calls == []


def test_python_script_shares_device_api_and_reports_errors():
    session = FakeSession("dev1", {"id=x": "value"})
    logs = []
    ok = PythonScript("dev", "def run(device):\n    print('got', device.copy_text('id', 'x'))\n")
    assert ScriptRunner(session, ok, on_log=lambda s, m: logs.append(m)).run().state == RunState.COMPLETED
    assert "got value" in logs

    broken = PythonScript("dev", "def run(device):\n    1 / 0\n")
    logs.clear()
    result = ScriptRunner(session, broken, on_log=lambda s, m: logs.append(m)).run()
    assert result.state == RunState.FAILED
    assert any("ZeroDivisionError" in line for line in logs)

    missing = PythonScript("dev", "x = 1\n")
    assert ScriptRunner(session, missing).run().state == RunState.FAILED


def test_stop_interrupts_python_script():
    session = FakeSession("dev1")
    stop = threading.Event()
    stop.set()
    script = PythonScript("dev", "def run(device):\n    device.open_app('a')\n")
    assert ScriptRunner(session, script, stop_event=stop).run().state == RunState.STOPPED
    assert session.calls == []
    # the stop hook is cleared once the run ends, so the session is reusable
    session.open_app("a")
    with pytest.raises(RunStopped):
        session.should_stop = lambda: True
        session.open_app("a")


def test_save_and_list_scripts(tmp_path):
    path = save_script({"name": "My Task!", "steps": [{"action": "wait", "seconds": "2"}]}, tmp_path)
    assert path.name == "My_Task.json"
    assert json.loads(path.read_text())["steps"] == [{"action": "wait", "seconds": 2}]
    assert load_script(path)["name"] == "My Task!"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "dev.py").write_text("def run(device): pass\n")
    entries = list_scripts(tmp_path)
    assert [e.display_name for e in entries] == ["My Task!", "dev  [Developer Script]"]
    with pytest.raises(ScriptError):
        save_script({"name": "bad", "steps": [{"action": "click"}]}, tmp_path)


def test_bundled_examples_are_valid():
    from core import paths
    for entry in list_scripts(paths.configs_dir()):
        if entry.kind == "json":
            load_script(entry.path)
