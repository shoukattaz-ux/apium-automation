"""Offscreen smoke tests for the PySide6 windows (no phone or Appium needed)."""

import json
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import paths  # noqa: E402
from core.manager import DeviceManager  # noqa: E402
from core.runner import RunState  # noqa: E402
from tests.test_core import FakeSession  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from ui.theme import STYLESHEET

    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(STYLESHEET)
    return application


@pytest.fixture
def configs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    (tmp_path / "configs" / "scripts").mkdir(parents=True)
    return tmp_path / "configs"


def wait_for(app, condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def make_window(monkeypatch, sessions):
    import ui.dashboard as dashboard

    monkeypatch.setattr(dashboard, "is_server_running", lambda url: True)
    manager = DeviceManager(session_factory=lambda serial, url: sessions[serial])
    window = dashboard.MainWindow(manager, scan_devices=False)
    window.appium_timer.stop()
    return window


def test_dashboard_runs_builder_script_per_device(app, configs, monkeypatch):
    from ui.script_editor import ScriptEditorWindow

    sessions = {s: FakeSession(s, {"id=src": f"value-{s}", "id=dst": ""}) for s in ("A1", "B2")}
    window = make_window(monkeypatch, sessions)
    window._apply_scan(["A1", "B2"], {"A1": "Pixel 7", "B2": "Galaxy S21"}, "")
    assert set(window.rows) == {"A1", "B2"}

    # Build and save a script through the form builder.
    editor = ScriptEditorWindow(window)
    editor.saved.connect(lambda _: window.reload_scripts())
    editor.builder.name_edit.setText("Copy value")
    editor.builder.steps = [
        {"action": "copy_text", "locator_type": "id", "locator_value": "src", "save_as": "v"},
        {"action": "paste_text", "locator_type": "id", "locator_value": "dst", "value_from": "v"},
    ]
    editor.builder._render()
    editor.builder.save()
    saved = configs / "Copy_value.json"
    assert json.loads(saved.read_text())["name"] == "Copy value"
    assert window.rows["A1"].script_combo.currentData() == str(saved)

    # Starting one device must not touch the other.
    window.start_device("A1")
    assert wait_for(app, lambda: window.rows["A1"].state == RunState.COMPLETED)
    assert sessions["A1"].screen["id=dst"] == "value-A1"
    assert sessions["B2"].screen["id=dst"] == ""
    assert window.rows["B2"].state == RunState.IDLE
    window.select_device("A1")
    assert "Completed" in window.log_view.toPlainText()

    # Unplugging marks the row disconnected and disables Start.
    window._apply_scan(["A1"], {}, "")
    assert not window.rows["B2"].connected
    assert not window.rows["B2"].start_button.isEnabled()
    editor.close()
    window.manager.shutdown()


def test_developer_mode_runs_and_saves(app, configs, monkeypatch):
    from ui.script_editor import ScriptEditorWindow, StepDialog

    sessions = {"A1": FakeSession("A1", {"id=x": "hello"})}
    window = make_window(monkeypatch, sessions)
    window._apply_scan(["A1"], {"A1": "Pixel"}, "")
    editor = ScriptEditorWindow(window)
    dev = editor.developer
    dev.refresh_devices()
    dev.name_edit.setText("dev test")
    dev.editor.setPlainText("def run(device):\n    print('text is', device.copy_text('id', 'x'))\n")
    dev.run()
    assert wait_for(app, lambda: window.rows["A1"].state == RunState.COMPLETED)
    assert any("text is hello" in line for line in window.logs["A1"])

    dev.editor.setPlainText("def run(device):\n    raise ValueError('boom')\n")
    dev.run()
    assert wait_for(app, lambda: window.rows["A1"].state == RunState.FAILED)
    assert any("ValueError: boom" in line for line in window.logs["A1"])

    dev.editor.setPlainText("def run(device:\n")
    dev.run()
    assert "Syntax error" in dev.message.text()

    dev.editor.setPlainText("def run(device):\n    pass\n")
    dev.save()
    assert (configs / "scripts" / "dev_test.py").exists()
    window.reload_scripts()
    assert window.rows["A1"].script_combo.findText("dev_test  [Developer Script]") >= 0

    # The step dialog shows inline errors instead of accepting bad input.
    dialog = StepDialog(editor, step={"action": "click", "locator_type": "id"})
    dialog._accept()
    assert dialog.result_step is None
    assert dialog.errors["locator_value"].text() == "Locator value is required"
    dialog.inputs["locator_value"].setText("com.app:id/ok")
    dialog._accept()
    assert dialog.result_step["locator_value"] == "com.app:id/ok"
    editor.close()
    window.manager.shutdown()
