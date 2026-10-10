"""Acting on the element that was picked, not merely on one a locator happens to match."""

from __future__ import annotations

from core.inspector import element_at, parse_page_source
from core.runner import ScriptRunner
from core.targeting import Candidate, check, choose, fingerprint, screen_signature, visible_and_settled
from tests.fakes import PAGE_SOURCE, FakeSession

# The "Pay now" row in PAGE_SOURCE: a clickable LinearLayout, 360×100 at [20,600][380,700].
ROW = {"class": "android.widget.LinearLayout", "resource-id": "com.shop:id/pay_row", "text": "",
       "content-desc": "", "bounds": "[20,600][380,700]"}
TARGET = {"class": "android.widget.LinearLayout", "id": "com.shop:id/pay_row", "text": "", "desc": "",
          "bounds": [20, 600, 380, 700], "screen": [400, 800]}


def run(session, steps):
    lines = []
    runner = ScriptRunner(session, {"name": "t", "steps": steps}, on_log=lambda serial, line: lines.append(line))
    runner._sleep = lambda seconds: None
    return runner.run(), lines


def test_fingerprint_and_screen_signature_from_the_picker():
    elements = parse_page_source(PAGE_SOURCE)
    row = next(e for e in elements if e.resource_id == "com.shop:id/pay_row")
    assert fingerprint(row, elements) == TARGET
    screen = screen_signature(row, elements)
    # Landmarks: uniquely labelled things in the top/bottom bands, never the row or what's inside it.
    assert ["accessibility id", "Menu"] in screen["anchors"] and all("Pay" not in a[1] for a in screen["anchors"])


def test_check_rejects_a_different_element_and_accepts_the_picked_one():
    assert check(TARGET, ROW)[0]
    assert check(TARGET, {**ROW, "bounds": "[20,100][380,200]"})[0]  # moved (list scrolled): still fine
    ok, reason, _ = check(TARGET, {**ROW, "class": "android.widget.TextView"})
    assert not ok and "different type" in reason
    ok, reason, _ = check(TARGET, {**ROW, "bounds": "[20,600][80,620]"})
    assert not ok and "different width" in reason
    ok, reason, _ = check({**TARGET, "text": "Video"}, {**ROW, "text": "Reels"})
    assert not ok and "Reels" in reason


def test_choose_between_look_alikes():
    near = Candidate("near", {**ROW})
    far = Candidate("far", {**ROW, "bounds": "[20,100][380,200]"})
    assert choose(TARGET, [far, near], (400, 800))[0] is near            # clearly at the picked place
    near_too = Candidate("near2", {**ROW, "bounds": "[20,620][380,720]"})
    chosen, reason = choose(TARGET, [near, near_too], (400, 800))
    assert chosen is None and "ambiguous" in reason                      # can't tell: don't guess
    chosen, reason = choose(None, [far, near])                           # older steps: first match, with a note
    assert chosen is far and "using the first" in reason


def test_visible_and_settled():
    assert visible_and_settled((0, 0, 10, 10), (0, 0, 10, 10), (400, 800)) == (True, "")
    assert not visible_and_settled((0, 0, 10, 10), (0, 40, 10, 50), (400, 800))[0]   # moving
    assert not visible_and_settled(None, (0, 900, 10, 950), (400, 800))[0]          # off screen
    assert not visible_and_settled(None, (0, 0, 2, 2), (400, 800))[0]               # hidden


def test_wrong_element_from_a_locator_is_rejected_and_the_next_one_used():
    session = FakeSession("S1", {"xpath=//weak": "", "id=com.shop:id/pay_row": ""})
    session.attrs["xpath=//weak"] = {**ROW, "class": "android.widget.ImageView", "bounds": "[0,0][40,40]"}
    session.attrs["id=com.shop:id/pay_row"] = dict(ROW)
    result, lines = run(session, [{
        "action": "click", "locator_type": "xpath", "locator_value": "//weak", "timeout_seconds": 0,
        "alternatives": [{"locator_type": "id", "locator_value": "com.shop:id/pay_row"}], "target": TARGET}])
    assert ("click", "id=com.shop:id/pay_row") in session.calls and ("click", "xpath=//weak") not in session.calls
    assert any("main locator found a different element" in line for line in lines)
    assert any("found with backup 2" in line for line in lines)
    assert not result.skipped_steps


def test_look_alikes_are_not_guessed():
    session = FakeSession("S1", {"text=Item": ""})
    session.matches["text=Item"] = [{**ROW, "bounds": "[20,600][380,700]"}, {**ROW, "bounds": "[20,610][380,710]"}]
    result, lines = run(session, [{"action": "click", "locator_type": "text", "locator_value": "Item",
                                   "timeout_seconds": 0, "target": TARGET}])
    assert not [c for c in session.calls if c[0] == "click"]
    assert result.skipped_steps and any("ambiguous" in line for line in lines)


def test_screen_check_stops_actions_on_the_wrong_screen():
    session = FakeSession("S1", {"id=com.shop:id/pay_row": "", "accessibility id=Menu": ""})
    session.attrs["id=com.shop:id/pay_row"] = dict(ROW)
    step = {"action": "click", "locator_type": "id", "locator_value": "com.shop:id/pay_row", "timeout_seconds": 0,
            "target": TARGET, "screen": {"package": "com.shop", "anchors": [["accessibility id", "Menu"]]}}
    session.package = "com.other"
    result, lines = run(session, [step])
    assert result.skipped_steps and any("wrong app on screen (com.other" in line for line in lines)
    session.package = "com.shop"
    del session.screen["accessibility id=Menu"]
    result, lines = run(session, [step])
    assert result.skipped_steps and any("not on the picked screen" in line for line in lines)
    session.screen["accessibility id=Menu"] = ""
    result, _ = run(session, [step])
    assert not result.skipped_steps and ("click", "id=com.shop:id/pay_row") in session.calls
    # Turned off on the step: acts regardless.
    session.package = "com.other"
    result, _ = run(session, [{**step, "check_screen": False}])
    assert not result.skipped_steps


def test_waits_for_a_moving_element_to_settle():
    session = FakeSession("S1", {"id=com.shop:id/pay_row": ""})
    session.attrs["id=com.shop:id/pay_row"] = dict(ROW)
    positions = iter(["[20,500][380,600]", "[20,600][380,700]"])  # first look: still scrolling
    session.bounds_of = lambda candidate: next(positions, candidate.attrs["bounds"])
    result, _ = run(session, [{"action": "click", "locator_type": "id", "locator_value": "com.shop:id/pay_row",
                               "timeout_seconds": 5, "target": TARGET}])
    assert not result.skipped_steps and ("click", "id=com.shop:id/pay_row") in session.calls


def test_backup_tap_only_where_the_picked_element_still_is():
    elements = parse_page_source(PAGE_SOURCE)
    assert element_at(elements, 100, 650).text == "Pay now"   # (25%, 81.25%) is inside the row
    step = {"action": "click", "locator_type": "id", "locator_value": "gone", "timeout_seconds": 0,
            "fallback_x": 25, "fallback_y": 81.25, "target": TARGET}
    session = FakeSession("S1")
    result, lines = run(session, [step])
    assert ("tap", 100, 650) in session.calls and not result.skipped_steps

    session = FakeSession("S1")
    result, lines = run(session, [{**step, "fallback_y": 8.75}])   # the "Order 1234" text is there instead
    assert not [c for c in session.calls if c[0] == "tap"] and result.skipped_steps
    assert any("backup position not used" in line and "Order 1234" in line for line in lines)


def test_lost_session_is_not_mistaken_for_a_bad_locator():
    from core.runner import RunState, _invalid_locator

    class InvalidSelectorException(Exception):
        pass

    class InvalidSessionIdException(Exception):
        pass

    assert _invalid_locator(InvalidSelectorException("x")) and _invalid_locator(ValueError("x"))
    assert not _invalid_locator(InvalidSessionIdException("invalid session id"))

    session = FakeSession("S1")

    def lost(*_):
        raise InvalidSessionIdException("invalid session id")
    session.candidates = lost
    result, lines = run(session, [{"action": "click", "locator_type": "id", "locator_value": "x",
                                   "timeout_seconds": 0}])
    assert result.state == RunState.FAILED and not any("not a valid locator" in line for line in lines)
