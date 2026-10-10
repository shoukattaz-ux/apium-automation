"""Making sure a step acts on the element that was picked, not merely on *an* element.

A locator matching something is not proof it matched the right thing: a positional
backup can land on another feed item, a text can also appear in a post, a popup can
cover the screen. So when an element is picked we keep:

* a **fingerprint** of it (class, id, text, description, size, position), and
* a **screen signature**: the app's package plus a few stable landmarks (tab bar,
  header) that were on screen.

At run time a locator's matches are checked against the fingerprint; ones that
don't fit are rejected and the next locator is tried, and the step waits until the
screen signature matches before acting. Everything here is pure logic (no phone),
so it is unit tested directly.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .inspector import UiElement, _xpath_literal

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

SIZE_TOLERANCE = 0.5        # a match may be 50%..200% of the picked element's width/height
NEAR = 0.08                 # centre distance (fraction of the screen diagonal) that counts as "same place"
FAR_APART = 0.15            # ...and the gap to the runner-up needed to break a tie by position
MAX_ANCHORS = 3


def parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return tuple(int(v) for v in value)  # type: ignore[return-value]
    match = _BOUNDS.match(str(value or ""))
    return tuple(int(v) for v in match.groups()) if match else None  # type: ignore[return-value]


@dataclass
class Candidate:
    """One element a locator matched on the phone."""

    handle: Any                         # what the session needs to act on it (a WebElement)
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def bounds(self) -> tuple[int, int, int, int] | None:
        return parse_bounds(self.attrs.get("bounds"))

    def label(self) -> str:
        kind = str(self.attrs.get("class") or "element").rsplit(".", 1)[-1]
        name = self.attrs.get("text") or self.attrs.get("content-desc") or ""
        return f"{kind} “{name[:30]}”" if name else kind


# ------------------------------------------------------------------ recording

def layout_size(elements: list[UiElement]) -> tuple[int, int]:
    """The screen extent the element bounds are measured in."""
    return (max((e.bounds[2] for e in elements), default=0), max((e.bounds[3] for e in elements), default=0))


def fingerprint(element: UiElement, elements: list[UiElement]) -> dict:
    """What the picked element looks like, for checking matches at run time."""
    width, height = layout_size(elements)
    return {
        "class": element.class_name,
        "id": element.resource_id,
        "text": element.text,
        "desc": element.content_desc,
        "bounds": list(element.bounds),
        "screen": [width, height],
    }


def screen_signature(element: UiElement, elements: list[UiElement]) -> dict:
    """The app and a few landmarks that identify the screen the element was picked on.

    Landmarks are uniquely labelled elements in the top quarter or bottom fifth of the
    screen (title bars, tab bars), which stay put while the content in the middle
    changes. The picked element and the elements around or inside it are left out.
    """
    width, height = layout_size(elements)
    package = element.attrs.get("package", "") or next(
        (e.attrs.get("package", "") for e in elements if e.attrs.get("package")), "")
    anchors: list[list[str]] = []
    if height:
        def related(other: UiElement) -> bool:
            return _contains(other.bounds, element.bounds) or _contains(element.bounds, other.bounds)

        candidates: list[tuple[int, list[str]]] = []
        for other in elements:
            if related(other) or other.area <= 0:
                continue
            centre_y = (other.bounds[1] + other.bounds[3]) / 2
            if not (centre_y < height * 0.25 or centre_y > height * 0.8):
                continue
            for rank, (attr, kind, value) in enumerate((
                    ("content-desc", "accessibility id", other.content_desc),
                    ("resource-id", "id", other.resource_id),
                    ("text", "text", other.text))):
                if value and len(value) <= 40 and sum(1 for e in elements if e.attrs.get(attr) == value) == 1:
                    candidates.append((rank, [kind, value]))
                    break
        seen: set[str] = set()
        for _, anchor in sorted(candidates, key=lambda c: c[0]):
            if anchor[1] not in seen:
                anchors.append(anchor)
                seen.add(anchor[1])
            if len(anchors) == MAX_ANCHORS:
                break
    return {"package": package, "anchors": anchors}


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def describe_target(target: dict) -> str:
    """e.g. 'ViewGroup 120×60 at (37%, 6%)' for the Edit Step dialog."""
    kind = str(target.get("class") or "element").rsplit(".", 1)[-1]
    name = target.get("text") or target.get("desc") or ""
    bounds = parse_bounds(target.get("bounds"))
    text = f"{kind} “{name[:30]}”" if name else kind
    if bounds:
        text += f" {bounds[2] - bounds[0]}×{bounds[3] - bounds[1]}"
        width, height = (target.get("screen") or [0, 0])[:2]
        if width and height:
            text += (f" at ({(bounds[0] + bounds[2]) / 2 / width * 100:.0f}%,"
                     f" {(bounds[1] + bounds[3]) / 2 / height * 100:.0f}%)")
    return text


def describe_screen(screen: dict) -> str:
    package = screen.get("package") or "any app"
    anchors = [f"“{a[1]}”" for a in screen.get("anchors") or []]
    return package + (f", showing {' or '.join(anchors)}" if anchors else "")


def anchor_xpath(anchor: list[str]) -> tuple[str, str]:
    """An anchor as a locator the session can look for."""
    kind, value = anchor[0], anchor[1]
    if kind == "text":
        return "xpath", f"//*[@text={_xpath_literal(value)}]"
    return kind, value


# ------------------------------------------------------------------ checking

def check(target: dict, attrs: dict, screen_size: tuple[int, int] | None = None) -> tuple[bool, str, float]:
    """Does a matched element fit the picked element's fingerprint?

    Returns (fits, reason when it doesn't, centre distance as a fraction of the screen
    diagonal; 0 when unknown). Identity (class, id, text, description) must match
    exactly; size must be within SIZE_TOLERANCE. Position isn't required to match
    (lists scroll), it only breaks ties.
    """
    def differs(key: str, attr: str) -> bool:
        return bool(target.get(key)) and str(attrs.get(attr) or "") != str(target[key])

    for key, attr, what in (("class", "class", "type"), ("id", "resource-id", "id"),
                            ("desc", "content-desc", "description"), ("text", "text", "text")):
        if differs(key, attr):
            return False, f"different {what}: {attrs.get(attr) or 'none'!r}", 1.0
    picked = parse_bounds(target.get("bounds"))
    found = parse_bounds(attrs.get("bounds"))
    if not picked or not found:
        return True, "", 0.0
    for picked_size, found_size, what in ((picked[2] - picked[0], found[2] - found[0], "width"),
                                          (picked[3] - picked[1], found[3] - found[1], "height")):
        if picked_size > 0 and not (SIZE_TOLERANCE <= found_size / picked_size <= 1 / SIZE_TOLERANCE):
            return False, f"different {what} ({found_size}px, picked {picked_size}px)", 1.0
    return True, "", _distance(target, picked, found, screen_size)


def _distance(target: dict, picked, found, screen_size) -> float:
    recorded = (target.get("screen") or [0, 0])[:2]
    now = screen_size or recorded
    if not (recorded[0] and recorded[1] and now[0] and now[1]):
        return 0.0
    px = ((picked[0] + picked[2]) / 2 / recorded[0], (picked[1] + picked[3]) / 2 / recorded[1])
    fx = ((found[0] + found[2]) / 2 / now[0], (found[1] + found[3]) / 2 / now[1])
    return math.hypot(px[0] - fx[0], px[1] - fx[1]) / math.sqrt(2)


def choose(target: dict | None, candidates: list[Candidate],
           screen_size: tuple[int, int] | None = None) -> tuple[Candidate | None, str]:
    """Pick the right element among a locator's matches, or explain why none is safe.

    Without a fingerprint (older steps) the first match is used, as before. With one,
    matches that don't fit are dropped; if several still fit, the one at the picked
    position wins only when it is clearly the closest, otherwise the locator is
    ambiguous and is skipped rather than guessing.
    """
    if not candidates:
        return None, "no match"
    if not target:
        return candidates[0], (f"matches {len(candidates)} elements, using the first" if len(candidates) > 1 else "")
    fitting: list[tuple[float, Candidate]] = []
    reasons: list[str] = []
    for candidate in candidates:
        ok, reason, distance = check(target, candidate.attrs, screen_size)
        if ok:
            fitting.append((distance, candidate))
        else:
            reasons.append(f"{candidate.label()} ({reason})")
    if not fitting:
        return None, "found a different element: " + "; ".join(reasons[:2])
    if len(fitting) == 1:
        return fitting[0][1], ""
    fitting.sort(key=lambda item: item[0])
    (best, chosen), (runner_up, _) = fitting[0], fitting[1]
    if best <= NEAR and runner_up - best >= FAR_APART:
        return chosen, ""
    return None, f"ambiguous: {len(fitting)} elements look like the picked one"


def visible_and_settled(before: tuple[int, int, int, int] | None, after: tuple[int, int, int, int] | None,
                        screen_size: tuple[int, int] | None) -> tuple[bool, str]:
    """Safe to tap: on screen, not tiny, and not moving (scrolling or animating)."""
    if not after:
        return True, ""  # bounds unavailable: nothing to check
    if after[2] - after[0] < 4 or after[3] - after[1] < 4:
        return False, "element is too small or hidden"
    if screen_size and screen_size[0] and screen_size[1]:
        centre = ((after[0] + after[2]) / 2, (after[1] + after[3]) / 2)
        if not (0 <= centre[0] < screen_size[0] and 0 <= centre[1] < screen_size[1]):
            return False, "element is off screen"
    if before and any(abs(a - b) > 3 for a, b in zip(before, after)):
        return False, "element is still moving"
    return True, ""
