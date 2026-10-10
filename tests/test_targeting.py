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
    assert any("1 of 2 locators agree: backup 2" in line for line in lines)
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


# ------------------------------------------------------------------ votes

def _row_at(top):
    return {**ROW, "bounds": f"[20,{top}][380,{top + 100}]"}


def _alts(*keys):
    return [{"locator_type": "xpath", "locator_value": key} for key in keys]


def test_most_votes_win_and_the_outvoted_locator_is_reported():
    session = FakeSession("S1", {f"xpath={k}": "" for k in ("//a", "//b", "//c", "//odd")})
    for key in ("xpath=//a", "xpath=//b", "xpath=//c"):
        session.attrs[key] = _row_at(600)
    session.attrs["xpath=//odd"] = _row_at(100)        # fits the fingerprint, but a different row
    result, lines = run(session, [{"action": "click", "locator_type": "xpath", "locator_value": "//a",
                                   "timeout_seconds": 0, "target": TARGET, "alternatives": _alts("//b", "//c", "//odd")}])
    assert not result.skipped_steps
    assert any("3 of 4 locators agree" in line for line in lines)
    assert any("backup 4 pointed at a different element" in line and "outvoted 3 to 1" in line for line in lines)


def test_a_tie_between_two_elements_is_refused():
    session = FakeSession("S1", {f"xpath={k}": "" for k in ("//a", "//b", "//c", "//d")})
    session.attrs["xpath=//a"] = session.attrs["xpath=//b"] = _row_at(600)
    session.attrs["xpath=//c"] = session.attrs["xpath=//d"] = _row_at(100)
    result, lines = run(session, [{"action": "click", "locator_type": "xpath", "locator_value": "//a",
                                   "timeout_seconds": 0, "target": TARGET, "alternatives": _alts("//b", "//c", "//d")}])
    assert result.skipped_steps and not [c for c in session.calls if c[0] == "click"]
    assert any("locators disagree" in line and "not guessing" in line for line in lines)


def test_quorum_default_and_setting():
    session = FakeSession("S1", {"xpath=//a": ""})
    session.attrs["xpath=//a"] = _row_at(600)
    step = {"action": "click", "locator_type": "xpath", "locator_value": "//a", "timeout_seconds": 0,
            "target": TARGET, "alternatives": _alts("//gone1", "//gone2")}
    result, lines = run(session, [step])   # 3 locators, only 1 matches: default needs 2
    assert result.skipped_steps and any("only 1 of 3 locators agree" in line and "needs 2" in line for line in lines)
    result, _ = run(session, [{**step, "min_agree": 1}])
    assert not result.skipped_steps
    result, lines = run(session, [{**step, "alternatives": _alts("//gone1")}])  # 2 locators: 1 is enough
    assert not result.skipped_steps


# ------------------------------------------------------------------ pictures

def _screen_png(icon_at=(240, 600), second_icon_at=None):
    """A 400×800 'screenshot' with a distinctive icon (120×80) drawn at ``icon_at``."""
    import io

    from PIL import Image, ImageDraw

    screen = Image.new("L", (400, 800), 225)
    draw = ImageDraw.Draw(screen)
    for i in range(12):
        draw.rectangle([10 + i * 30, 300 + (i % 3) * 40, 30 + i * 30, 320 + (i % 3) * 40], fill=40 + i * 15)
    icon = Image.new("L", (120, 80), 255)
    icon_draw = ImageDraw.Draw(icon)
    icon_draw.ellipse([20, 10, 100, 70], outline=0, width=6)
    icon_draw.line([35, 40, 85, 40], fill=0, width=6)
    for at in (icon_at, second_icon_at):
        if at:
            screen.paste(icon, at)
    out = io.BytesIO()
    screen.save(out, "PNG")
    return out.getvalue(), icon


def _save_icon(tmp_path, icon, monkeypatch):
    from core import paths

    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    (tmp_path / "configs" / "images").mkdir(parents=True)
    icon.save(tmp_path / "configs" / "images" / "icon.png")
    return {"locator_type": "image", "locator_value": "images/icon.png"}


ICON_TARGET = {**TARGET, "bounds": [240, 600, 360, 680]}


def test_picture_adds_a_vote_only_when_it_looks_the_same(tmp_path, monkeypatch):
    png, icon = _screen_png()
    picture = _save_icon(tmp_path, icon, monkeypatch)
    session = FakeSession("S1", {"xpath=//a": "", "xpath=//b": ""})
    session.shot = png
    session.attrs["xpath=//a"] = session.attrs["xpath=//b"] = {**ROW, "bounds": "[240,600][360,680]"}
    step = {"action": "click", "locator_type": "xpath", "locator_value": "//a", "timeout_seconds": 0,
            "target": ICON_TARGET, "alternatives": [{"locator_type": "xpath", "locator_value": "//b"}, picture]}
    result, lines = run(session, [step])
    assert not result.skipped_steps and any("3 of 3 locators agree: main locator, backup 2, picture" in l for l in lines)

    # The locators found something at a place that doesn't look like the picked element.
    session.shot, _ = _screen_png(icon_at=(20, 20))
    result, lines = run(session, [step])
    assert any("picture doesn't look like what the locators found" in line for line in lines)
    assert any("2 of 3 locators agree" in line for line in lines) and not result.skipped_steps


def test_picture_rescues_a_click_only_when_found_once(tmp_path, monkeypatch):
    png, icon = _screen_png(icon_at=(240, 600))
    picture = _save_icon(tmp_path, icon, monkeypatch)
    step = {"action": "click", "locator_type": "xpath", "locator_value": "//gone", "timeout_seconds": 0,
            "target": ICON_TARGET, "alternatives": [picture]}
    session = FakeSession("S1")
    session.shot = png
    result, lines = run(session, [step])
    assert ("tap", 300, 640) in session.calls and not result.skipped_steps   # centre of the icon
    assert any("found by its picture" in line for line in lines)

    session = FakeSession("S1")
    session.shot, _ = _screen_png(icon_at=(240, 600), second_icon_at=(20, 100))
    result, lines = run(session, [step])
    assert not [c for c in session.calls if c[0] == "tap"] and result.skipped_steps
    assert any("appears more than once" in line for line in lines)


# ------------------------------------------------------------------ layout snapshot & image matching

def test_every_picker_locator_finds_the_same_element_in_a_layout_snapshot():
    from core.inspector import suggest_locators
    from core.localfind import LocalScreen
    from tests.test_core import FB_LITE_TABS

    for source in (PAGE_SOURCE, FB_LITE_TABS):
        snapshot, elements = LocalScreen(source), parse_page_source(source)
        for element in elements:
            for suggestion in suggest_locators(element, elements):
                found = snapshot.evaluate(suggestion.locator_type, suggestion.locator_value)
                assert found is not None, suggestion
                if suggestion.unique:
                    assert [f["bounds"] for f in found] == ["[%d,%d][%d,%d]" % element.bounds], suggestion
                else:
                    assert len(found) == suggestion.matches, suggestion
    snapshot = LocalScreen(PAGE_SOURCE)
    assert snapshot.evaluate("android uiautomator", 'new UiSelector().scrollable(true)') is None  # ask the phone
    assert len(snapshot.evaluate("id", "item")) == 2  # bare id: the app's package is implied
    import pytest
    with pytest.raises(ValueError):
        snapshot.evaluate("xpath", "//*[")


def test_session_uses_one_snapshot_for_all_locators():
    from core.devices import DeviceSession, LocalHandle

    class Driver:
        page_source = PAGE_SOURCE
        calls = 0

        def find_elements(self, *_):
            Driver.calls += 1
            return []

        def tap(self, points):
            self.tapped = points

    session = DeviceSession("S1", connect=False)
    session.driver = Driver()
    session.begin_pass()
    rows = session.candidates("id", "com.shop:id/pay_row")
    assert Driver.calls == 0 and isinstance(rows[0].handle, LocalHandle)
    assert rows[0].attrs["class"] == "android.widget.LinearLayout"
    assert session.text_of(session.candidates("text", "Pay now")[0]) == "Pay now"
    session.click_candidate(rows[0])
    assert session.driver.tapped == [(200, 650)]  # centre of [20,600][380,700]


def test_image_search_and_check():
    import numpy as np

    from core import imagematch

    png, icon = _screen_png(icon_at=(240, 600))
    shot, template = imagematch.load_gray(png), np.asarray(icon, np.float32) / 255
    match = imagematch.search(shot, template)
    assert match and match.unique and match.bounds == (240, 600, 360, 680) and match.score > 0.99
    assert imagematch.similarity(shot, template, (240, 600, 360, 680)) > 0.99
    assert imagematch.similarity(shot, template, (20, 20, 140, 100)) < 0.5
    twice, _ = _screen_png(icon_at=(240, 600), second_icon_at=(20, 100))
    assert not imagematch.search(imagematch.load_gray(twice), template).unique
    flat = np.full((80, 120), 0.5, np.float32)
    assert imagematch.search(shot, flat) is None  # a plain patch can't identify anything
    assert imagematch.worth_keeping((0, 0, 100, 60), (400, 800))
    assert not imagematch.worth_keeping((0, 0, 8, 8), (400, 800))
    assert not imagematch.worth_keeping((0, 0, 400, 700), (400, 800))
