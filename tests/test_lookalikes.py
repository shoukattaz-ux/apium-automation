"""Problem 1: a whole page selected instead of the icon. Problem 2: 50 clones named "DualSpace"
that differ only in a badge number drawn into the icon."""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image, ImageDraw, ImageFont

from core import imagematch
from core.inspector import clickable_target, element_at, parse_page_source
from core.runner import ScriptRunner
from core.targeting import look_alikes
from tests.fakes import FakeSession

CELL, COLS, TOP = (160, 170), 4, 100
WIDTH, HEIGHT = COLS * CELL[0] + 40, 1400


def _font(size):
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def launcher(numbers, with_tiktok=True):
    """A launcher page: a big clickable 'Home screen 1' holding non-clickable icons (TextViews),
    each clone a grey icon with a white badge showing its number. Returns (png, xml, {n: bounds})."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (40, 30, 25))
    draw = ImageDraw.Draw(image)
    nodes, bounds = [], {}
    items = (["tiktok"] if with_tiktok else []) + list(numbers)
    for i, item in enumerate(items):
        x, y = 20 + (i % COLS) * CELL[0], TOP + (i // COLS) * CELL[1]
        box = (x, y, x + CELL[0], y + CELL[1])
        draw.rounded_rectangle([x + 20, y + 10, x + 140, y + 130], 25, fill=(70, 70, 75))
        if item == "tiktok":
            draw.polygon([(x + 60, y + 30), (x + 100, y + 70), (x + 60, y + 110)], fill=(250, 40, 90))
            label = "TikTok Lite"
        else:
            draw.ellipse([x + 35, y + 35, x + 60, y + 60], fill=(230, 230, 230))
            draw.ellipse([x + 80, y + 15, x + 135, y + 70], fill=(250, 250, 250))
            draw.text((x + 107, y + 42), str(item), fill=(10, 10, 10), font=_font(26), anchor="mm")
            label = "DualSpace"
            bounds[item] = box
        draw.text((x + 80, y + 150), label, fill=(240, 240, 240), font=_font(18), anchor="mm")
        if item == "tiktok":
            bounds["tiktok"] = box
        nodes.append(f'<android.widget.TextView class="android.widget.TextView" text="{label}" '
                     f'content-desc="{label}" clickable="false" package="com.android.launcher3" '
                     f'bounds="[{box[0]},{box[1]}][{box[2]},{box[3]}]"/>')
    xml = ("<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"0\">"
           f'<android.widget.FrameLayout class="android.widget.FrameLayout" package="com.android.launcher3" '
           f'bounds="[0,0][{WIDTH},{HEIGHT}]">'
           f'<android.view.ViewGroup class="android.view.ViewGroup" content-desc="Home screen 1" clickable="true" '
           f'package="com.android.launcher3" bounds="[0,{TOP - 20}][{WIDTH},{HEIGHT - 60}]">'
           + "".join(nodes) + "</android.view.ViewGroup></android.widget.FrameLayout></hierarchy>")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue(), xml, bounds


NUMBERS = [2, 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 49, 48]


# ------------------------------------------------------------------ problem 1

def test_a_label_never_turns_into_the_whole_page():
    _, xml, bounds = launcher(NUMBERS)
    elements = parse_page_source(xml)
    left, top, right, bottom = bounds["tiktok"]
    icon = element_at(elements, (left + right) / 2, (top + bottom) / 2)
    assert icon.text == "TikTok Lite"
    assert clickable_target(icon, elements) is icon  # not "Home screen 1"


def test_clicking_a_large_container_is_refused_unless_allowed():
    session = FakeSession("S1", {"accessibility id=Home screen 1": ""})
    session.attrs["accessibility id=Home screen 1"] = {"class": "android.view.ViewGroup", "bounds": "[0,80][400,760]"}
    lines = []
    step = {"action": "click", "locator_type": "accessibility id", "locator_value": "Home screen 1", "timeout_seconds": 0}
    result = ScriptRunner(session, {"name": "t", "steps": [step]}, on_log=lambda s, l: lines.append(l)).run()
    assert result.skipped_steps and not [c for c in session.calls if c[0] == "click"]
    assert any("Refusing to tap" in line and "covers 85% of the screen" in line for line in lines)
    result = ScriptRunner(session, {"name": "t", "steps": [{**step, "allow_large": True}]}).run()
    assert not result.skipped_steps and ("click", "accessibility id=Home screen 1") in session.calls


# ------------------------------------------------------------------ problem 2

def _picked_step(target_number):
    """What the picker records for clone ``target_number`` (through the real picker code paths)."""
    png, xml, bounds = launcher(NUMBERS)
    elements = parse_page_source(xml)
    left, top, right, bottom = bounds[target_number]
    clone = element_at(elements, (left + right) / 2, (top + bottom) / 2)
    alikes = look_alikes(clone, elements)
    assert len(alikes) == len(NUMBERS) - 1
    patch, box = imagematch.distinguishing_mark(png, clone.bounds, [a.bounds for a in alikes], (WIDTH, HEIGHT))
    assert box[0] > 0.4 and box[3] < 0.5  # the badge: top-right of the icon
    return patch, box, clone


@pytest.fixture
def clone_step(tmp_path, monkeypatch):
    from core import paths

    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    folder = tmp_path / "configs" / "images"
    folder.mkdir(parents=True)

    def make(number):
        patch, box, clone = _picked_step(number)
        mark = imagematch.save_template(patch, folder)
        target = {"class": clone.class_name, "id": "", "text": "DualSpace", "desc": "DualSpace",
                  "bounds": list(clone.bounds), "screen": [WIDTH, HEIGHT], "mark": {"image": mark, "box": list(box)}}
        return {"action": "click", "locator_type": "text", "locator_value": "DualSpace", "timeout_seconds": 0,
                "target": target}
    return make


def _session_showing(numbers):
    png, _, bounds = launcher(numbers)
    session = FakeSession("S1", {"text=DualSpace": ""})
    session.shot = png
    session.window_size = lambda: (WIDTH, HEIGHT)
    session.matches["text=DualSpace"] = [
        {"class": "android.widget.TextView", "text": "DualSpace", "content-desc": "DualSpace",
         "bounds": "[%d,%d][%d,%d]" % bounds[n], "number": str(n)} for n in numbers]
    return session


def _clicked_number(session, numbers):
    clicks = [c for c in session.calls if c[0] == "click"]
    return numbers[clicks[0][2]] if clicks else None


@pytest.mark.parametrize("number", [17, 1, 18, 49])
def test_the_right_clone_is_found_after_the_order_changes(clone_step, number):
    shuffled = NUMBERS[:]
    random.Random(number).shuffle(shuffled)
    session = _session_showing(shuffled)
    result = ScriptRunner(session, {"name": "t", "steps": [clone_step(number)]}).run()
    assert not result.skipped_steps and _clicked_number(session, shuffled) == number


def test_a_missing_clone_is_refused_not_swapped(clone_step):
    others = [n for n in NUMBERS if n != 49]
    session = _session_showing(others)
    lines = []
    result = ScriptRunner(session, {"name": "t", "steps": [clone_step(49)]},
                          on_log=lambda s, l: lines.append(l)).run()
    assert result.skipped_steps and _clicked_number(session, others) is None
    assert any("distinguishing mark" in line for line in lines)


def test_scrolls_down_to_find_a_clone_further_down(clone_step, monkeypatch):
    import core.runner as runner_module

    monkeypatch.setattr(runner_module, "SCROLL_LOOK_SECONDS", 0)
    first_page, second_page = NUMBERS[:8], NUMBERS[8:]
    session = _session_showing(first_page)

    def scroll(direction="down", times=1):
        session.calls.append(("scroll", direction, times))
        replacement = _session_showing(second_page)
        session.shot, session.matches = replacement.shot, replacement.matches

    session.scroll = scroll
    lines = []
    runner = ScriptRunner(session, {"name": "t", "steps": [{**clone_step(49), "scroll_search": 3}]},
                          on_log=lambda s, l: lines.append(l))
    runner._sleep = lambda seconds: None
    result = runner.run()
    assert ("scroll", "down", 1) in session.calls and not result.skipped_steps
    assert _clicked_number(session, second_page) == 49
    assert any("scrolling to look for it (1/3)" in line for line in lines)


def test_picker_records_the_distinguishing_mark(app, window_factory, configs):
    from tests.test_ui import wait_for
    from ui.inspector import InspectorWindow

    png, xml, bounds = launcher(NUMBERS)
    session = FakeSession("A1")
    session.shot = png
    session.page_source = lambda: xml
    window = window_factory({"A1": session})
    steps = []
    picker = InspectorWindow(window, add_steps=steps.extend, serial="A1")
    assert wait_for(app, lambda: bool(picker.elements))

    left, top, right, bottom = bounds[17]
    picker._click((left + right) / 2, (top + bottom) / 2)
    assert "look-alike" in picker.status.text()
    picker._add_step("click")
    step = steps[-1]
    assert (step["locator_type"], step["locator_value"]) in {("text", "DualSpace"), ("accessibility id", "DualSpace")}
    mark = step["target"]["mark"]
    assert (configs / mark["image"]).is_file() and mark["box"][0] > 0.4

    left, top, right, bottom = bounds["tiktok"]
    picker.record.setChecked(False)
    picker._click(left + 5, top + 5)
    assert picker.selected.text == "TikTok Lite"
    picker._click(10, HEIGHT - 100)  # empty area of the big page: warn
    assert "large container" in picker.status.text()
    picker.close()
