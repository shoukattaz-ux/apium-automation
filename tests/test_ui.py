"""Offscreen tests for the PySide6 windows (no phone or Appium needed)."""

import json
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import paths  # noqa: E402
from core.manager import DeviceManager  # noqa: E402
from core.runner import RunState  # noqa: E402
from core.scheduler import Schedule, ScheduleStore  # noqa: E402
from tests.fakes import FakeSession  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from ui.theme import apply_theme

    application = QApplication.instance() or QApplication([])
    apply_theme(application)
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


@pytest.fixture
def window_factory(app, configs, monkeypatch):
    import ui.dashboard as dashboard

    monkeypatch.setattr(dashboard, "is_server_running", lambda url: True)
    created = []

    def make(sessions, models=None):
        manager = DeviceManager(session_factory=lambda serial, url: sessions[serial])
        window = dashboard.MainWindow(manager, scan_devices=False, schedules=ScheduleStore(configs / "schedules.json"))
        window.appium_timer.stop()
        window._apply_scan(list(sessions), models or {s: f"Phone {s}" for s in sessions}, "")
        created.append(window)
        return window

    yield make
    for window in created:
        window.manager.shutdown()
        for child in list(window._windows):
            child.close()


def test_builder_script_runs_per_device_and_lands_in_history(app, configs, window_factory):
    from ui.script_editor import ScriptEditorWindow

    sessions = {s: FakeSession(s, {"id=src": f"value-{s}", "id=dst": ""}) for s in ("A1", "B2")}
    window = window_factory(sessions)

    editor = ScriptEditorWindow(window)
    editor.saved.connect(lambda _: window.reload_scripts())
    editor.builder.name_edit.setText("Copy value")
    editor.builder.steps = [
        {"action": "copy_text", "locator_type": "id", "locator_value": "src", "save_as": "v"},
        {"action": "repeat", "times": 2, "steps": [
            {"action": "paste_text", "locator_type": "id", "locator_value": "dst", "text": "{{v}}-{{loop_index}}"},
        ]},
    ]
    editor.builder.render()
    path = editor.builder.save()
    assert path == configs / "Copy_value.json"
    assert json.loads(path.read_text())["steps"][1]["steps"][0]["text"] == "{{v}}-{{loop_index}}"
    window.rows["A1"].script_combo.setCurrentIndex(window.rows["A1"].script_combo.findData(str(path)))

    window.start_device("A1")
    assert wait_for(app, lambda: window.rows["A1"].state == RunState.COMPLETED)
    assert sessions["A1"].screen["id=dst"] == "value-A1-2"
    assert sessions["B2"].screen["id=dst"] == ""
    assert window.rows["B2"].state == RunState.IDLE
    window.select_device("A1")
    assert "Completed" in window.log_view.toPlainText()

    assert wait_for(app, lambda: window.history.table.rowCount() == 1)
    assert window.history.table.item(0, 3).text() == "Done"

    window._apply_scan(["A1"], {}, "")
    assert not window.rows["B2"].connected
    assert not window.rows["B2"].start_button.isEnabled()
    editor.close()


def test_nested_builder_cards_and_step_dialog(app, configs, window_factory):
    from ui.script_editor import ScriptEditorWindow, StepCard, StepDialog

    window = window_factory({"A1": FakeSession("A1")})
    editor = ScriptEditorWindow(window)
    builder = editor.builder
    builder.load({"name": "n", "steps": [
        {"action": "if_exists", "locator_type": "text", "locator_value": "OK", "device": "A",
         "then": [{"action": "click", "locator_type": "text", "locator_value": "OK", "device": "A"}],
         "else": [{"action": "press_key", "key": "back", "device": "B"}]},
    ]})
    cards = builder.cards_host.findChildren(StepCard)
    assert len(cards) == 3
    assert "Cross-phone workflow" in builder.mode_label.text()

    # Editing the block keeps its nested steps.
    dialog = StepDialog(editor, step=builder.steps[0], roles=["A", "B"])
    dialog.inputs["locator_value"].setText("Okay")
    dialog._accept()
    assert dialog.result_step["then"][0]["locator_value"] == "OK"
    assert dialog.result_step["locator_value"] == "Okay"

    # Inline validation for bad input; advanced options are offered for device steps.
    dialog = StepDialog(editor, step={"action": "click", "locator_type": "id"})
    assert "retries" in dialog.inputs and "device" in dialog.inputs
    dialog._accept()
    assert dialog.result_step is None
    assert dialog.errors["locator_value"].text() == "Locator value is required"
    dialog.inputs["locator_value"].setText("com.app:id/ok")
    dialog.inputs["retries"].setValue(2)
    dialog._accept()
    assert dialog.result_step["retries"] == 2
    assert "fallback_x" not in dialog.result_step  # optional number left empty stays off
    dialog = StepDialog(editor, step={"action": "click", "locator_type": "id", "locator_value": "x",
                                      "fallback_x": 50, "fallback_y": 12.5})
    assert dialog.inputs["fallback_x"].value() == 50
    dialog._accept()
    assert (dialog.result_step["fallback_x"], dialog.result_step["fallback_y"]) == (50, 12.5)

    builder.duplicate_step(builder.steps, 0)
    builder.move_step(builder.steps[0]["then"], 0, 1)  # no-op, single child
    builder.steps[1]["then"].clear()
    builder.save()
    problems = builder.steps_error.text()
    assert "add at least one step" in problems
    editor.close()


def test_run_dialog_starts_cross_phone_workflow(app, configs, window_factory):
    from ui.run_dialog import RunDialog

    sessions = {"P1": FakeSession("P1", {"id=order": "#42"}), "P2": FakeSession("P2", {"id=note": ""})}
    window = window_factory(sessions)
    (configs / "wf.json").write_text(json.dumps({"name": "wf", "steps": [
        {"action": "copy_text", "locator_type": "id", "locator_value": "order", "save_as": "o", "device": "A"},
        {"action": "paste_text", "locator_type": "id", "locator_value": "note", "text": "got {{o}}", "device": "B"},
    ]}))
    window.reload_scripts()
    dialog = RunDialog(window, window.scripts, window.connected_devices(), set(), configs / "wf.json", "P1")
    assert set(dialog.picker.role_combos) == {"A", "B"}
    assert dialog.picker.mapping() == {"A": "P1", "B": "P2"}
    dialog.picker.role_combos["B"].setCurrentIndex(dialog.picker.role_combos["B"].findData("P1"))
    dialog._accept()
    assert dialog.result_request is None and "different phone" in dialog.error.text()
    dialog.picker.role_combos["B"].setCurrentIndex(dialog.picker.role_combos["B"].findData("P2"))
    dialog.repeat.setValue(2)
    dialog._accept()
    request = dialog.result_request
    assert request.mapping == {"A": "P1", "B": "P2"} and request.repeat == 2

    assert window.execute(request) == 1
    assert wait_for(app, lambda: not window.manager.running_serials()
                    and window.rows["P1"].state == RunState.COMPLETED)
    assert sessions["P2"].screen["id=note"] == "got #42"
    assert wait_for(app, lambda: window.history.table.rowCount() == 2)


def test_inspector_picks_and_records(app, configs, window_factory):
    from ui.inspector import InspectorWindow
    from ui.script_editor import ScriptEditorWindow

    session = FakeSession("A1", {"text=Pay now": ""})
    window = window_factory({"A1": session})
    editor = ScriptEditorWindow(window)
    inspector = InspectorWindow(window, add_steps=editor.builder.append_steps, roles=["A"], serial="A1")
    assert wait_for(app, lambda: bool(inspector.elements))
    inspector._click(100, 70)
    assert inspector.selected.text == "Order 1234"
    assert inspector.current_locator() == ("id", "com.shop:id/order")
    inspector._add_step("copy_text")
    assert editor.builder.steps[-1]["action"] == "copy_text"

    inspector.record.setChecked(True)
    inspector.role_combo.setCurrentText("A")
    inspector._click(100, 650)  # the label inside the clickable row
    assert wait_for(app, lambda: ("click", "id=com.shop:id/pay_row") in session.calls or
                    any(c[0] == "tap" for c in session.calls))
    recorded = editor.builder.steps[-1]
    assert recorded["action"] == "click" and recorded["device"] == "A"
    assert recorded["locator_value"] == "com.shop:id/pay_row"
    inspector.close()

    picked = []
    picker = InspectorWindow(window, on_pick=lambda t, v: picked.append((t, v)), serial="A1")
    assert wait_for(app, lambda: bool(picker.elements))
    picker._click(380, 20)
    picker._use_locator()
    assert picked == [("accessibility id", "Menu")]
    editor.close()


def test_schedules_run_and_dialogs(app, configs, window_factory):
    from ui.schedules import ScheduleEditDialog, SchedulesDialog

    session = FakeSession("A1")
    window = window_factory({"A1": session})
    (configs / "tick.json").write_text(json.dumps({"name": "tick", "steps": [{"action": "open_app", "package": "x"}]}))
    window.reload_scripts()

    schedule = Schedule(name="Every hour", script="tick.json", kind="interval", every_minutes=60, serials=["A1"])
    window.schedules.upsert(schedule)
    window.check_schedules()  # never run before -> due now
    assert wait_for(app, lambda: ("open_app", "x") in session.calls)
    assert window.schedules.schedules[0].last_run
    assert "Every hour" in window.schedule_label.text()
    window.check_schedules()  # not due again yet
    assert wait_for(app, lambda: not window.manager.running_serials())
    assert session.calls.count(("open_app", "x")) == 1

    offline = Schedule(name="Offline", script="tick.json", kind="interval", serials=["ZZ"])
    assert window.run_schedule(offline) == 0
    assert any("not connected" in line for line in window.logs["ZZ"])

    dialog = SchedulesDialog(window, window.schedules)
    assert dialog.table.rowCount() == 1
    edit = ScheduleEditDialog(dialog, schedule, window.connected_devices())
    edit.every.setValue(15)
    edit._accept()
    assert edit.result_schedule.every_minutes == 15 and edit.result_schedule.id == schedule.id
    blank = ScheduleEditDialog(dialog, None, [])
    blank._accept()
    assert blank.result_schedule is None and "name" in blank.error.text()


def test_developer_mode_runs_and_saves(app, configs, window_factory):
    from ui.script_editor import ScriptEditorWindow

    window = window_factory({"A1": FakeSession("A1", {"id=x": "hello"})})
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
    editor.close()
