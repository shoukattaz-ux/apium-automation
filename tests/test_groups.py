"""Script groups, the Errors / Stuck log, the alarm and the library's duplicate/rename/delete."""

import json
import threading
import time

import pytest

from core import errorlog
from core.alerts import Alerter, builtin_ringtone, wait_until_silent
from core.errorlog import ErrorEntry, ErrorLog
from core.groups import Group, group_from_dict, load_group, save_group, validate_group
from core.library import delete_script, duplicate_script, list_scripts, references_to, rename_script
from core.manager import DeviceManager
from core.runner import RunState, load_any_script
from core.schema import ScriptError
from tests.fakes import FakeSession

OK = {"name": "Ok", "steps": [{"action": "click", "locator_type": "id", "locator_value": "go"}]}
BAD = {"name": "Bad", "steps": [{"action": "click", "locator_type": "id", "locator_value": "missing",
                                 "timeout_seconds": 0, "on_fail": "stop"}]}
SLOW = {"name": "Slow", "steps": [{"action": "wait", "seconds": 30}]}
WORKFLOW = {"name": "Two", "steps": [{"action": "wait", "seconds": 0, "device": "A"},
                                     {"action": "wait", "seconds": 0, "device": "B"}]}


def _write(configs, name, data):
    (configs / name).write_text(json.dumps(data), encoding="utf-8")
    return name


def _wait_idle(manager, serial, timeout=15):
    deadline = time.monotonic() + timeout
    while manager.is_running(serial) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not manager.is_running(serial)


class Rings:
    def __init__(self):
        self.played = []

    def __call__(self, path, stop):
        self.played.append(path)
        stop.wait(0.01)


def _manager(tmp_path, session, alerter=None):
    log = ErrorLog(tmp_path / "errors.jsonl")
    manager = DeviceManager(session_factory=lambda serial, url: session, history_dir=tmp_path / "runs",
                            error_log=log, alerter=alerter)
    finished = []
    manager.add_finished_listener(lambda serials, results: finished.append(results))
    return manager, log, finished


# --------------------------------------------------------------------- group files


def test_group_file_round_trip_and_validation(configs):
    _write(configs, "ok.json", OK)
    _write(configs, "two.json", WORKFLOW)
    group = Group(name="Morning", scripts=["ok.json"], retries=2, max_minutes=5)
    path = save_group(group)
    assert path.parent == configs / "groups"
    loaded = load_group(path)
    assert loaded == group and loaded.path == path
    assert isinstance(load_any_script(path), Group)
    # a second group with the same name gets its own file
    assert save_group(Group(name="Morning", scripts=["ok.json"])) != path

    assert validate_group(Group(name="x", scripts=["nope.json"]))[0].startswith("Script not found")
    assert any("several phones" in p for p in validate_group(Group(name="x", scripts=["two.json"])))
    assert any("another group" in p for p in validate_group(Group(name="x", scripts=["groups/" + path.name])))
    assert validate_group(Group(name="", scripts=[]))  # name and scripts required
    with pytest.raises(ScriptError):
        group_from_dict({"name": "x", "scripts": ["a.json"], "on_script_fail": "explode"})


# --------------------------------------------------------------------- running groups


def test_group_runs_scripts_in_order_and_skips_a_failing_one(configs, tmp_path):
    for name, data in (("a.json", OK), ("bad.json", BAD), ("c.json", OK)):
        _write(configs, name, data)
    session = FakeSession("s1", {"id=go": ""})
    rings = Rings()
    alerter = Alerter(player=rings)
    manager, log, finished = _manager(tmp_path, session, alerter)
    group = Group(name="G", scripts=["a.json", "bad.json", "c.json"], retries=2, pause_seconds=0)

    assert manager.start("s1", group)
    assert _wait_idle(manager, "s1")
    results = finished[0]
    # a ok, bad tried 3 times (1 + 2 retries) then skipped, c still ran
    assert [r.state for r in results] == [RunState.COMPLETED, RunState.FAILED, RunState.COMPLETED]
    assert session.calls.count(("click", "id=go")) == 2
    status = manager.status.get("s1")
    assert status["state"] == RunState.PARTIAL and "Bad" in status["message"]

    entries = log.entries()
    kinds = [e.kind for e in entries]
    assert kinds.count(errorlog.STEP_FAILED) == 3          # one per attempt, with the step
    assert kinds[0] == errorlog.SCRIPT_FAILED               # newest: the script gave up
    final = entries[0]
    assert final.group == "G" and final.script == "Bad" and final.attempts == 3
    step_entry = next(e for e in entries if e.kind == errorlog.STEP_FAILED)
    assert "missing" in step_entry.step and step_entry.screenshot.endswith(".png")
    wait_until_silent(alerter)
    assert rings.played  # the alarm rang


def test_group_set_to_stop_ends_at_the_failing_script(configs, tmp_path):
    for name, data in (("bad.json", BAD), ("c.json", OK)):
        _write(configs, name, data)
    session = FakeSession("s1", {"id=go": ""})
    manager, log, finished = _manager(tmp_path, session)
    group = Group(name="G", scripts=["bad.json", "c.json"], retries=0, on_script_fail="stop", alert=False)
    assert manager.start("s1", group)
    assert _wait_idle(manager, "s1")
    assert [r.state for r in finished[0]] == [RunState.FAILED]
    assert ("click", "id=go") not in session.calls
    assert manager.status.get("s1")["state"] == RunState.FAILED


def test_flaky_script_passes_on_retry(configs, tmp_path):
    _write(configs, "a.json", OK)
    session = FakeSession("s1", {"id=go": ""}, fail_times={"id=go": 1})
    manager, log, finished = _manager(tmp_path, session)
    strict = dict(OK, steps=[dict(OK["steps"][0], timeout_seconds=0, on_fail="stop")])
    _write(configs, "a.json", strict)
    assert manager.start("s1", Group(name="G", scripts=["a.json"], retries=1, pause_seconds=0))
    assert _wait_idle(manager, "s1")
    assert finished[0][-1].state == RunState.COMPLETED
    assert manager.status.get("s1")["state"] == RunState.COMPLETED
    assert not [e for e in log.entries() if e.kind == errorlog.SCRIPT_FAILED]


def test_stuck_script_is_stopped_logged_and_skipped(configs, tmp_path):
    _write(configs, "slow.json", SLOW)
    _write(configs, "c.json", OK)
    session = FakeSession("s1", {"id=go": ""})
    manager, log, finished = _manager(tmp_path, session)
    group = Group(name="G", scripts=["slow.json", "c.json"], retries=0, max_minutes=0.01, pause_seconds=0,
                  alert=False)
    started = time.monotonic()
    assert manager.start("s1", group)
    assert _wait_idle(manager, "s1")
    assert time.monotonic() - started < 10
    assert [r.state for r in finished[0]] == [RunState.FAILED, RunState.COMPLETED]
    stuck = [e for e in log.entries() if e.kind == errorlog.STUCK]
    assert stuck and stuck[0].script == "Slow" and stuck[0].screenshot.endswith(".png")


def test_stopping_a_group_stops_the_current_script(configs, tmp_path):
    _write(configs, "slow.json", SLOW)
    session = FakeSession("s1")
    manager, log, finished = _manager(tmp_path, session)
    assert manager.start("s1", Group(name="G", scripts=["slow.json", "slow.json"], alert=False))
    time.sleep(0.4)
    manager.stop("s1")
    assert _wait_idle(manager, "s1", timeout=5)
    assert manager.status.get("s1")["state"] == RunState.STOPPED
    assert len(finished[0]) == 1


def test_missing_script_in_a_group_is_skipped(configs, tmp_path):
    _write(configs, "a.json", OK)
    session = FakeSession("s1", {"id=go": ""})
    manager, log, finished = _manager(tmp_path, session)
    group = Group(name="G", scripts=["a.json"], pause_seconds=0, alert=False)
    group.scripts.insert(0, "gone.json")  # deleted after the group was saved
    assert manager.start("s1", group)
    assert _wait_idle(manager, "s1")
    assert [r.state for r in finished[0]] == [RunState.FAILED, RunState.COMPLETED]


# --------------------------------------------------------------------- alarm and errors log


def test_alerter_respects_settings_and_stops(monkeypatch, tmp_path, configs):
    from core import settings as app_settings

    rings = Rings()
    alerter = Alerter(player=rings)
    events = []
    alerter.listeners.append(lambda ringing, reason: events.append(ringing))
    monkeypatch.setattr(app_settings, "_current", app_settings.Settings(alert_enabled=False))
    assert not alerter.alert("x")
    assert alerter.alert("test", force=True, sound=str(builtin_ringtone(tmp_path)), repeats=2)
    wait_until_silent(alerter)
    assert len(rings.played) == 2 and events == [True, False]

    slow = threading.Event()
    alerter = Alerter(player=lambda path, stop: stop.wait(5) or slow.set())
    monkeypatch.setattr(app_settings, "_current", app_settings.Settings(alert_enabled=True, alert_repeats=5))
    assert alerter.alert("ring")
    assert alerter.ringing
    alerter.stop()
    assert not alerter.ringing


def test_builtin_ringtone_is_a_wav(tmp_path):
    import wave

    path = builtin_ringtone(tmp_path)
    with wave.open(str(path)) as audio:
        assert audio.getnframes() / audio.getframerate() > 0.5


def test_error_log_keeps_newest_first_and_clears(tmp_path):
    log = ErrorLog(tmp_path / "e.jsonl")
    seen = []
    log.listeners.append(seen.append)
    for i in range(3):
        log.add(ErrorEntry(kind=errorlog.STEP_SKIPPED, device="s1", script=f"s{i}", reason="r"))
    (tmp_path / "e.jsonl").open("a").write("not json\n")
    assert [e.script for e in log.entries()] == ["s2", "s1", "s0"]
    assert len(seen) == 3
    log.clear()
    assert log.entries() == []


# --------------------------------------------------------------------- library housekeeping


def test_library_lists_groups_and_duplicates_renames_deletes(configs):
    _write(configs, "ok.json", OK)
    (configs / "scripts" / "dev.py").write_text("pass\n", encoding="utf-8")
    save_group(Group(name="Chain", scripts=["ok.json", "scripts/dev.py"]))
    (configs / "schedules.json").write_text(json.dumps([{"name": "Morning", "script": "ok.json"}]))

    entries = list_scripts()
    group = entries[0]
    assert group.is_group and group.display_name == "Chain  [group · 2 scripts]"
    assert {e.name for e in entries} == {"Chain", "Ok", "dev"}

    copy = duplicate_script(configs / "ok.json")
    assert json.loads(copy.read_text())["name"] == "Ok (copy)"
    second = duplicate_script(configs / "ok.json")
    assert json.loads(second.read_text())["name"] == "Ok (copy 2)"
    assert duplicate_script(configs / "scripts" / "dev.py").name == "dev_copy.py"
    group_copy = duplicate_script(group.path)
    assert load_group(group_copy).name == "Chain (copy)"

    rename_script(copy, "Renamed")
    assert json.loads(copy.read_text())["name"] == "Renamed"
    with pytest.raises(ScriptError):
        rename_script(configs / "scripts" / "dev.py", "x")

    uses = references_to(configs / "ok.json")
    assert "Schedule: Morning" in uses and "Group: Chain" in uses
    moved = delete_script(configs / "ok.json")
    assert not (configs / "ok.json").exists() and moved.parent == configs / "deleted" and moved.is_file()
    assert "Ok" not in {e.name for e in list_scripts()}
