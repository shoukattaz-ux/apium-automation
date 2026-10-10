import json
import threading
from datetime import datetime

import pytest

from core.devices import RunStopped, parse_adb_devices
from core.history import list_runs
from core.inspector import best_locator, clickable_target, element_at, parse_page_source, suggest_locators
from core.library import list_scripts
from core.manager import DeviceManager
from core.runner import PythonScript, RunState, ScriptRunner, StatusBoard, run_script_on_devices
from core.history import RunRecorder
from core.scheduler import Schedule, ScheduleStore
from core.schema import (
    ScriptError, describe_step, load_script, normalize_step, render, save_script, script_roles,
    validate_script, validate_step,
)
from tests.fakes import PAGE_SOURCE, BrokenSession, FakeSession

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


def run(session, script, **kwargs):
    return ScriptRunner(session, script, **kwargs).run()


# --------------------------------------------------------------------- basics


def test_parse_adb_devices():
    output = "List of devices attached\nABC123\tdevice\nXYZ\tunauthorized\n\n"
    assert parse_adb_devices(output) == (["ABC123"], {"XYZ": "unauthorized"})


def test_validation():
    assert validate_step({"action": "click", "locator_type": "id"}) == {"locator_value": "Locator value is required"}
    assert "value_from" in validate_step({"action": "paste_text", "locator_type": "id", "locator_value": "x"})
    assert validate_step({"action": "scroll", "direction": "sideways", "times": 1})
    assert validate_step({"action": "tap", "x": 150, "y": 10}) == {"x": "X (%) must be at most 100"}
    assert validate_step({"action": "click", "locator_type": "id", "locator_value": "x", "on_fail": "explode"})
    assert validate_step({"action": "set_variable", "name": "bad name"})
    assert validate_script(SCRIPT) == []
    assert validate_script({"name": "", "steps": []})
    nested = {"name": "n", "steps": [{"action": "repeat", "times": 2, "steps": [{"action": "click"}]}]}
    assert any(p.startswith("Step 1.1 (Click)") for p in validate_script(nested))
    empty_block = {"name": "n", "steps": [{"action": "repeat", "times": 2, "steps": []}]}
    assert any("add at least one step" in p for p in validate_script(empty_block))


def test_render_and_describe():
    assert render("Order {{ id }} for {{name}} {{missing}}", {"id": 7, "name": "Ann"}) == "Order 7 for Ann {{missing}}"
    assert "2 retries" in describe_step({"action": "click", "locator_type": "id", "locator_value": "x", "retries": 2})
    step = normalize_step({"action": "click", "locator_type": "id", "locator_value": " x ", "retries": 0,
                           "on_fail": "skip", "device": ""})
    assert step == {"action": "click", "locator_type": "id", "locator_value": "x"}


# --------------------------------------------------------------------- runner


def test_runner_skips_missing_element_and_continues():
    session = FakeSession("dev1", {"id=a:id/title": "hello", "id=b:id/input": ""})
    logs = []
    result = run(session, SCRIPT, on_log=lambda s, m: logs.append(m))
    assert result.state == RunState.PARTIAL
    assert result.skipped_steps == ["3"]
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
    assert run(session, SCRIPT, stop_event=stop).state == RunState.STOPPED
    assert session.calls == []


def test_retries_then_success_and_on_fail_stop(tmp_path):
    session = FakeSession("dev1", {"id=btn": ""}, fail_times={"id=btn": 2})
    script = {"name": "r", "steps": [
        {"action": "click", "locator_type": "id", "locator_value": "btn", "retries": 2},
        {"action": "click", "locator_type": "id", "locator_value": "nope", "on_fail": "stop"},
        {"action": "open_app", "package": "never"},
    ]}
    recorder = RunRecorder("r", {"device": "dev1"}, base_dir=tmp_path)
    runner = ScriptRunner(session, script, recorder=recorder)
    runner._sleep = lambda seconds: None  # don't wait between retries in tests
    result = runner.run()
    assert result.state == RunState.FAILED
    assert "Step 2 failed" in result.error
    assert ("click", "id=btn") in session.calls
    assert ("open_app", "never") not in session.calls
    steps = recorder.record.steps
    assert [s.status for s in steps] == ["ok", "failed"]
    assert steps[0].attempts == 3
    assert steps[1].screenshot and (result.report / steps[1].screenshot).exists()
    assert (result.report / "report.html").read_text().count("<tr>") >= 3


def test_control_flow_repeat_if_and_variables():
    session = FakeSession("dev1", {"text=Paid": "", "id=out": ""})
    script = {"name": "flow", "steps": [
        {"action": "set_variable", "name": "who", "value": "Ann"},
        {"action": "repeat", "times": 3, "steps": [
            {"action": "paste_text", "locator_type": "id", "locator_value": "out",
             "text": "{{who}} #{{loop_index}}"},
        ]},
        {"action": "if_exists", "locator_type": "text", "locator_value": "Paid",
         "then": [{"action": "set_variable", "name": "status", "value": "paid"}],
         "else": [{"action": "set_variable", "name": "status", "value": "open"}]},
        {"action": "if_exists", "locator_type": "text", "locator_value": "Refunded",
         "then": [{"action": "set_variable", "name": "refund", "value": "yes"}]},
        {"action": "press_key", "key": "back"},
        {"action": "tap", "x": 50, "y": 25},
        {"action": "stop_run", "message": "done for {{who}}"},
        {"action": "open_app", "package": "never"},
    ]}
    result = run(session, script)
    assert result.state == RunState.COMPLETED
    assert result.message == "Ended by Stop Script step: done for Ann"
    pastes = [c[2] for c in session.calls if c[0] == "paste"]
    assert pastes == ["Ann #1", "Ann #2", "Ann #3"]
    assert result.variables["status"] == "paid"
    assert "refund" not in result.variables
    assert ("key", "back") in session.calls and ("tap", 200, 200) in session.calls
    assert ("open_app", "never") not in session.calls


def test_cross_phone_workflow_shares_variables(tmp_path):
    phone_a = FakeSession("serialA", {"id=order": "#1234"})
    phone_b = FakeSession("serialB", {"id=note": ""})
    script = {"name": "wf", "steps": [
        {"action": "copy_text", "locator_type": "id", "locator_value": "order", "save_as": "order", "device": "A"},
        {"action": "paste_text", "locator_type": "id", "locator_value": "note", "text": "Order {{order}}",
         "device": "B"},
        {"action": "screenshot", "name": "after", "device": "B"},
    ]}
    assert script_roles(script) == ["A", "B"]
    logs = []
    recorder = RunRecorder("wf", {"A": "serialA", "B": "serialB"}, base_dir=tmp_path)
    result = run({"A": phone_a, "B": phone_b}, script, on_log=lambda s, m: logs.append((s, m)),
                 recorder=recorder)
    assert result.state == RunState.COMPLETED
    assert phone_b.screen["id=note"] == "Order #1234"
    assert any(s == "serialB" and "Paste" in m for s, m in logs)
    assert recorder.record.steps[-1].screenshot
    with pytest.raises(ScriptError):
        ScriptRunner({"A": phone_a}, script)


def test_session_lost_fails_run():
    result = run(BrokenSession("dev1"), {"name": "b", "steps": [{"action": "open_app", "package": "x"}]})
    assert result.state == RunState.FAILED
    assert result.error.startswith("Lost connection")


def test_python_script_shares_device_api_and_reports_errors():
    session = FakeSession("dev1", {"id=x": "value"})
    logs = []
    ok = PythonScript("dev", "def run(device):\n    print('got', device.copy_text('id', 'x'))\n")
    assert run(session, ok, on_log=lambda s, m: logs.append(m)).state == RunState.COMPLETED
    assert "got value" in logs

    logs.clear()
    result = run(session, PythonScript("dev", "def run(device):\n    1 / 0\n"), on_log=lambda s, m: logs.append(m))
    assert result.state == RunState.FAILED
    assert any("ZeroDivisionError" in line for line in logs)
    assert run(session, PythonScript("dev", "x = 1\n")).state == RunState.FAILED


def test_stop_interrupts_python_script():
    session = FakeSession("dev1")
    stop = threading.Event()
    stop.set()
    assert run(session, PythonScript("dev", "def run(device):\n    device.open_app('a')\n"),
               stop_event=stop).state == RunState.STOPPED
    assert session.calls == []
    session.open_app("a")  # the stop hook is cleared once the run ends
    with pytest.raises(RunStopped):
        session.should_stop = lambda: True
        session.open_app("a")


# --------------------------------------------------------------------- manager


def _wait_idle(manager, serials, timeout=10):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(manager.is_running(s) for s in serials):
            return True
        time.sleep(0.02)
    return False


def test_manager_workflow_locks_phones_and_repeats(tmp_path):
    phones = {"s1": FakeSession("s1", {"id=v": "x"}), "s2": FakeSession("s2", {"id=in": ""})}
    manager = DeviceManager(session_factory=lambda serial, url: phones[serial], history_dir=tmp_path)
    finished = []
    manager.add_finished_listener(lambda serials, results: finished.append((serials, results)))
    script = {"name": "wf", "steps": [
        {"action": "copy_text", "locator_type": "id", "locator_value": "v", "save_as": "v", "device": "A"},
        {"action": "wait", "seconds": 0.2},
        {"action": "paste_text", "locator_type": "id", "locator_value": "in", "value_from": "v", "device": "B"},
    ]}
    with pytest.raises(ScriptError):
        manager.start("s1", script)  # needs a phone per role
    with pytest.raises(ScriptError):
        manager.start_workflow({"A": "s1", "B": "s1"}, script)
    assert manager.start_workflow({"A": "s1", "B": "s2"}, script, repeat=2)
    assert manager.is_running("s2")
    assert not manager.start("s2", SCRIPT)  # busy as part of the workflow
    assert _wait_idle(manager, ["s1", "s2"])
    assert finished and [r.state for r in finished[0][1]] == [RunState.COMPLETED, RunState.COMPLETED]
    runs = list_runs(tmp_path)
    assert len(runs) == 2 and runs[0][1].devices == {"A": "s1", "B": "s2"}
    assert manager.status.get("s2")["state"] == RunState.COMPLETED


def test_manager_stop_ends_repeat_forever(tmp_path):
    phones = {"s1": FakeSession("s1")}
    manager = DeviceManager(session_factory=lambda serial, url: phones[serial], history_dir=tmp_path)
    script = {"name": "loop", "steps": [{"action": "wait", "seconds": 0.05}]}
    assert manager.start("s1", script, repeat=0, delay_seconds=0.05)
    import time
    time.sleep(0.4)
    manager.stop("s1")
    assert _wait_idle(manager, ["s1"])
    assert len(list_runs(tmp_path)) >= 2


# --------------------------------------------------------------------- inspector


def test_inspector_picks_element_and_locators():
    elements = parse_page_source(PAGE_SOURCE)
    order = element_at(elements, 100, 70)
    assert order.text == "Order 1234"
    assert best_locator(order, elements).locator_type == "id"

    label = element_at(elements, 100, 650)
    assert label.text == "Pay now"
    assert clickable_target(label, elements).resource_id == "com.shop:id/pay_row"
    assert best_locator(label, elements) == suggest_locators(label, elements)[0]
    assert best_locator(label, elements).locator_type == "text"

    item = element_at(elements, 100, 290)
    best = best_locator(item, elements)
    assert best.unique and best.locator_type == "xpath" and best.locator_value.endswith("[2]")

    menu = element_at(elements, 380, 20)
    assert best_locator(menu, elements).locator_value == "Menu"
    assert element_at(elements, 1000, 1000) is None


# --------------------------------------------------------------------- scheduler


def test_schedule_due_logic(tmp_path):
    daily = Schedule(name="d", script="x.json", kind="daily", time="09:00", weekdays=[0, 1, 2, 3, 4],
                     serials=["s1"])
    monday = datetime(2026, 10, 5)  # a Monday
    assert not daily.is_due(monday.replace(hour=8, minute=59))
    assert daily.is_due(monday.replace(hour=9, minute=5))
    assert not daily.is_due(monday.replace(hour=11))  # too late to catch up
    daily.last_run = monday.replace(hour=9, minute=0, second=3).isoformat()
    assert not daily.is_due(monday.replace(hour=9, minute=10))
    assert daily.next_run(monday.replace(hour=10)) == datetime(2026, 10, 6, 9, 0)
    assert not daily.is_due(datetime(2026, 10, 10, 9, 1))  # Saturday

    interval = Schedule(name="i", script="x.json", kind="interval", every_minutes=30, serials=["s1"])
    now = datetime(2026, 10, 5, 12, 0)
    assert interval.is_due(now)
    interval.last_run = now.isoformat()
    assert not interval.is_due(datetime(2026, 10, 5, 12, 29))
    assert interval.is_due(datetime(2026, 10, 5, 12, 30))

    store = ScheduleStore(tmp_path / "schedules.json")
    store.upsert(daily)
    store.upsert(interval)
    assert [s.name for s in ScheduleStore(tmp_path / "schedules.json").schedules] == ["d", "i"]
    store.remove(daily.id)
    assert [s.name for s in ScheduleStore(tmp_path / "schedules.json").schedules] == ["i"]
    assert Schedule(name="", script="", kind="daily", time="25:00", weekdays=[]).validate()


# --------------------------------------------------------------------- files


def test_save_and_list_scripts(tmp_path):
    path = save_script({"name": "My Task!", "steps": [{"action": "wait", "seconds": "2"}]}, tmp_path)
    assert path.name == "My_Task.json"
    assert json.loads(path.read_text())["steps"] == [{"action": "wait", "seconds": 2}]
    assert load_script(path)["name"] == "My Task!"
    save_script({"name": "Two phones", "steps": [
        {"action": "open_app", "package": "a", "device": "A"},
        {"action": "open_app", "package": "b", "device": "B"}]}, tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "dev.py").write_text("def run(device): pass\n")
    entries = list_scripts(tmp_path)
    assert [e.display_name for e in entries] == ["My Task!", "Two phones  [A + B workflow]",
                                                 "dev  [Developer Script]"]
    with pytest.raises(ScriptError):
        save_script({"name": "bad", "steps": [{"action": "click"}]}, tmp_path)


def test_bundled_examples_are_valid():
    from core import paths
    entries = [e for e in list_scripts(paths.configs_dir()) if e.kind == "json"]
    assert len(entries) >= 2
    for entry in entries:
        load_script(entry.path)


FB_LITE_TABS = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy index="0" rotation="0" width="400" height="800">
  <android.widget.FrameLayout bounds="[0,0][400,800]">
    <android.view.ViewGroup bounds="[0,0][400,60]">
      <android.view.ViewGroup clickable="true" bounds="[0,0][100,60]">
        <android.view.View content-desc="Home" bounds="[30,10][70,50]"/>
      </android.view.ViewGroup>
      <android.view.ViewGroup clickable="true" bounds="[100,0][200,60]">
        <android.view.ViewGroup bounds="[110,5][190,55]">
          <android.view.View content-desc="Video" bounds="[130,10][170,50]"/>
        </android.view.ViewGroup>
      </android.view.ViewGroup>
    </android.view.ViewGroup>
  </android.widget.FrameLayout>
</hierarchy>"""


def test_unlabelled_element_is_found_through_its_labelled_child():
    import xml.etree.ElementTree as ET

    elements = parse_page_source(FB_LITE_TABS)
    tab = next(e for e in elements if e.bounds == (100, 0, 200, 60))
    suggestions = suggest_locators(tab, elements)
    best = suggestions[0]
    assert best == best_locator(tab, elements)
    assert best.locator_value == '//*[@content-desc="Video"]/../..' and not best.fragile
    assert suggestions[-1].fragile and suggestions[-1].locator_value == tab.xpath
    # The anchored path really points at the tab.
    root = ET.fromstring(FB_LITE_TABS.split("?>", 1)[1])
    found = root.findall(".//*[@content-desc='Video']/../..")
    assert [n.attrib["bounds"] for n in found] == ["[100,0][200,60]"]


def test_click_falls_back_to_saved_position():
    session = FakeSession("S1", screen={})
    lines = []
    script = {"name": "fallback", "steps": [
        {"action": "click", "locator_type": "xpath", "locator_value": "//gone", "timeout_seconds": 0,
         "fallback_x": 50, "fallback_y": 25},
        {"action": "click", "locator_type": "xpath", "locator_value": "//gone", "timeout_seconds": 0},
    ]}
    result = ScriptRunner(session, script, on_log=lambda *args: lines.append(str(args[-1]))).run()
    assert ("tap", 200, 200) in session.calls  # 50% × 400, 25% × 800
    assert any("tapped its saved position (50%, 25%)" in line for line in lines)
    assert len(result.skipped_steps) == 1  # the step without a fallback is still skipped

    assert validate_step({"action": "click", "locator_type": "id", "locator_value": "x", "fallback_x": 5}) == {
        "fallback_y": "Set both X and Y for the backup tap, or neither"}


def test_backup_locators_are_tried_in_order():
    session = FakeSession("S1", screen={"text=Video": "Video", "xpath=//backup": "b"})
    lines = []
    script = {"name": "alts", "steps": [
        {"action": "click", "locator_type": "id", "locator_value": "gone", "timeout_seconds": 0,
         "alternatives": [{"locator_type": "xpath", "locator_value": "//also-gone"},
                          {"locator_type": "text", "locator_value": "Video"},
                          {"locator_type": "xpath", "locator_value": "//backup"}]},
        {"action": "if_exists", "locator_type": "id", "locator_value": "gone", "timeout_seconds": 0,
         "alternatives": [{"locator_type": "xpath", "locator_value": "//backup"}],
         "then": [{"action": "press_key", "key": "back"}]},
        {"action": "click", "locator_type": "id", "locator_value": "gone", "timeout_seconds": 0,
         "alternatives": [{"locator_type": "id", "locator_value": "nope"}], "fallback_x": 10, "fallback_y": 10},
    ]}
    result = ScriptRunner(session, script, on_log=lambda *args: lines.append(str(args[-1]))).run()
    assert ("click", "text=Video") in session.calls and ("click", "xpath=//backup") not in session.calls
    assert any("found with backup 3: text=Video" in line for line in lines)
    assert ("key", "back") in session.calls
    assert ("tap", 40, 80) in session.calls  # no locator matched: backup position
    assert not result.skipped_steps

    step = {"action": "click", "locator_type": "android uiautomator",
            "locator_value": "/android.widget.FrameLayout/android.view.ViewGroup[2]"}
    assert "XPath" in validate_step(step)["locator_value"]
    assert "XPath" in validate_step({"action": "click", "locator_type": "xpath", "locator_value": "//a",
                                     "alternatives": [{"locator_type": "id", "locator_value": "(//a)[1]"}]}
                                    )["alternatives"]
    saved = normalize_step({"action": "copy_text", "locator_type": "id", "locator_value": "a", "save_as": "x",
                            "alternatives": [{"locator_type": "text", "locator_value": " b "}, {"locator_type": "id"}]})
    assert saved["alternatives"] == [{"locator_type": "text", "locator_value": "b"}]
    assert "+1 backup locator" in describe_step(saved)


def test_picker_offers_every_locator_type():
    elements = parse_page_source(PAGE_SOURCE)
    order = element_at(elements, 100, 70)
    found = suggest_locators(order, elements)
    assert {s.locator_type for s in found} == {"id", "text", "android uiautomator", "xpath"}
    assert all(s.unique for s in found) and [s.fragile for s in found][-2:] == [True, True]
    assert ("android uiautomator", 'new UiSelector().resourceId("com.shop:id/order")') in \
        [(s.locator_type, s.locator_value) for s in found]
    # Look-alike rows: positional locators are the unique ones, the plain id is not.
    item = element_at(elements, 100, 290)
    unique = [(s.locator_type, s.locator_value) for s in suggest_locators(item, elements) if s.unique]
    assert ("android uiautomator", 'new UiSelector().resourceId("com.shop:id/item").instance(1)') in unique
    assert ("id", "com.shop:id/item") not in unique
