"""Offscreen tests for the PySide6 windows (no phone or Appium needed)."""

import json
import time

import pytest

pytest.importorskip("PySide6")

from core.runner import RunState  # noqa: E402
from core.scheduler import Schedule  # noqa: E402
from tests.fakes import FakeSession  # noqa: E402


def wait_for(app, condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False



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
    assert dialog.picker.problems() == []  # one phone may play several roles
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
    inspector.locators.setCurrentRow(inspector.locators.count() - 1)  # highlight the fragile full path
    inspector._add_step("click")
    fragile_pick = editor.builder.steps.pop()
    assert fragile_pick["locator_value"] == "com.shop:id/order"  # a stable locator stays the main one
    tail = [alt["locator_value"] for alt in fragile_pick["alternatives"][-2:]]  # fragile ones last
    assert tail[0].startswith("/hierarchy/android.widget.FrameLayout") and ".instance(" in tail[1]
    inspector.locators.setCurrentRow(1)  # a stable one is honoured as main
    inspector._add_step("click")
    assert editor.builder.steps.pop()["locator_type"] == "text"
    inspector.locators.setCurrentRow(0)
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

    assert recorded["alternatives"][-1]["locator_type"] == "xpath"  # every other unique locator kept as backup
    assert (recorded["fallback_x"], recorded["fallback_y"]) == (50.0, 81.2)  # centre of [20,600][380,700]

    # Picking for the Edit Step dialog: all unique locators (highlighted one first) and the position.
    from tests.test_targeting import _screen_png

    session.shot, _ = _screen_png(icon_at=(330, 0))  # a realistic 400×800 screenshot
    picked = []
    picker = window.open_inspector(on_pick=picked.append)
    assert picker.isModal()  # otherwise the modal Edit Step dialog blocks it
    assert wait_for(app, lambda: bool(picker.elements))
    picker._click(380, 20)
    picker._use_locator()
    menu_xpath = "/hierarchy/android.widget.FrameLayout/android.widget.ImageButton"
    locators, position = picked[0]["locators"], picked[0]["position"]
    assert picked[0]["target"]["class"] == "android.widget.ImageButton" and picked[0]["target"]["desc"] == "Menu"
    assert picked[0]["screen"]["anchors"]  # landmarks recorded with the screen
    assert locators[0] == ("accessibility id", "Menu") and locators[-1] == ("xpath", menu_xpath)
    assert {t for t, _ in locators} == {"accessibility id", "android uiautomator", "xpath", "class name", "image"}
    picture = next(v for t, v in locators if t == "image")
    assert (configs / picture).is_file() and locators.index(("image", picture)) == len(locators) - 3  # before fragile
    assert position == (92.5, 2.5)

    from ui.script_editor import StepDialog

    dialog = StepDialog(editor, step={"action": "click", "locator_type": "id", "locator_value": "old"},
                        pick_locator=lambda apply: apply(picked[0]))
    assert not dialog.verify_box.isEnabled()  # nothing recorded yet
    dialog._pick()
    assert dialog.verify_box.isEnabled() and dialog.verify_box.isChecked()
    assert "ImageButton" in dialog.verify_label.text()
    assert dialog.inputs["locator_type"].currentText() == "accessibility id"
    assert dialog.inputs["locator_value"].text() == "Menu"
    backups = [{"locator_type": t, "locator_value": v} for t, v in locators[1:]]
    assert dialog.alternatives.locators() == backups
    assert dialog.inputs["fallback_x"].value() == 92.5
    dialog.alternatives.value_edit.setText("Menu button")
    dialog.alternatives.type_combo.setCurrentText("text")
    dialog.alternatives._add()
    dialog.alternatives.list.setCurrentRow(dialog.alternatives.list.count() - 1)
    while dialog.alternatives.list.currentRow() > 0:
        dialog.alternatives._move(-1)
    dialog._promote_alternative()  # "text=Menu button" becomes main, Menu goes to the backups
    dialog.screen_box.setChecked(False)
    assert dialog.agree.value() == 0 and dialog.agree.text() == "Auto"
    dialog.agree.setValue(3)
    from PySide6.QtCore import Qt

    image_rows = [i for i in range(dialog.alternatives.list.count())
                  if dialog.alternatives.list.item(i).data(Qt.UserRole)[0] == "image"]
    assert image_rows and not dialog.alternatives.list.item(image_rows[0]).icon().isNull()  # thumbnail shown
    dialog._accept()
    assert dialog.result_step["min_agree"] == 3
    assert dialog.result_step["target"]["desc"] == "Menu" and "verify" not in dialog.result_step
    assert dialog.result_step["check_screen"] is False and dialog.result_step["screen"]
    assert dialog.result_step["locator_type"] == "text"
    assert dialog.result_step["alternatives"] == [{"locator_type": "accessibility id", "locator_value": "Menu"}] \
        + backups
    dialog.alternatives._add()
    dialog.alternatives.value_edit.setText("//bad")
    dialog.alternatives.type_combo.setCurrentText("id")
    dialog.alternatives._add()
    dialog._accept()
    assert "XPath" in dialog.alternatives_error.text()
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


def test_step_names_and_roles_to_names(app, configs, window_factory):
    from core.schema import describe_step, roles_to_titles
    from ui.script_editor import ScriptEditorWindow, StepDialog

    window = window_factory({})
    editor = ScriptEditorWindow(window)
    editor.builder.steps = [
        {"action": "click", "locator_type": "text", "locator_value": "Video", "device": "video tab"},
        {"action": "repeat", "times": 2, "device": "loop", "steps": [
            {"action": "press_key", "key": "back", "device": "go back", "title": "Back"}]},
        {"action": "wait", "seconds": 1},
    ]
    editor.builder.render()
    assert editor.builder.roles_button.isVisibleTo(editor)
    changed = roles_to_titles(editor.builder.steps)
    editor.builder.render()
    assert changed == 3 and not editor.builder.roles()
    assert not editor.builder.roles_button.isVisibleTo(editor)
    assert editor.builder.steps[0]["title"] == "video tab" and "device" not in editor.builder.steps[0]
    assert editor.builder.steps[1]["steps"][0]["title"] == "Back (go back)"
    assert describe_step(editor.builder.steps[0]) == "video tab — Click text=Video"

    # Step name is the first field, saved with the step; the dialog never outgrows the screen.
    dialog = StepDialog(editor, step=editor.builder.steps[0])
    assert dialog.inputs["title"].text() == "video tab"
    dialog.inputs["title"].setText("Open Video tab")
    dialog._accept()
    assert dialog.result_step["title"] == "Open Video tab"
    screen = dialog.screen().availableGeometry().height()
    assert dialog.height() <= screen
    editor.close()
