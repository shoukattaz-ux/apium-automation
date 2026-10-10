"""Element picker logic: parse a screen's UI hierarchy and suggest locators.

UiAutomator2's page source is XML where each node is an on-screen view::

    <android.widget.Button index="0" text="Pay" resource-id="com.shop:id/pay"
        content-desc="" class="android.widget.Button" clickable="true"
        bounds="[48,1620][1032,1764]" .../>

``bounds`` use the same pixel coordinates as a screenshot, so a click on the
screenshot maps straight onto elements.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class UiElement:
    tag: str
    attrs: dict[str, str]
    bounds: tuple[int, int, int, int]  # left, top, right, bottom
    xpath: str
    depth: int
    children: list["UiElement"] = field(default_factory=list, repr=False)

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(0, right - left) * max(0, bottom - top)

    def contains(self, x: float, y: float) -> bool:
        left, top, right, bottom = self.bounds
        return left <= x < right and top <= y < bottom

    @property
    def resource_id(self) -> str:
        return self.attrs.get("resource-id", "")

    @property
    def text(self) -> str:
        return self.attrs.get("text", "")

    @property
    def content_desc(self) -> str:
        return self.attrs.get("content-desc", "")

    @property
    def class_name(self) -> str:
        return self.attrs.get("class", self.tag)

    @property
    def clickable(self) -> bool:
        return self.attrs.get("clickable") == "true"

    def label(self) -> str:
        """Short human description, e.g. 'Button “Pay” (com.shop:id/pay)'."""
        kind = self.class_name.rsplit(".", 1)[-1]
        name = self.text or self.content_desc
        rid = self.resource_id.split("/", 1)[-1] if self.resource_id else ""
        parts = [kind]
        if name:
            parts.append(f"“{name[:40]}”")
        if rid:
            parts.append(f"({rid})")
        return " ".join(parts)


@dataclass(frozen=True)
class LocatorSuggestion:
    locator_type: str
    locator_value: str
    matches: int  # how many elements on screen it matches
    fragile: bool = False  # positional path: breaks when the layout shifts (scrolling, new items)

    @property
    def unique(self) -> bool:
        return self.matches == 1


def parse_page_source(xml_text: str) -> list[UiElement]:
    """Flatten the hierarchy into a list of elements (document order)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Could not read the screen layout: {exc}") from exc

    elements: list[UiElement] = []

    def walk(node: ET.Element, path: str, depth: int, parent: UiElement | None) -> None:
        counts: dict[str, int] = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            index = counts[child.tag]
            same = sum(1 for sibling in node if sibling.tag == child.tag)
            child_path = f"{path}/{child.tag}" + (f"[{index}]" if same > 1 else "")
            match = _BOUNDS.match(child.attrib.get("bounds", ""))
            element = None
            if match:
                bounds = tuple(int(v) for v in match.groups())
                element = UiElement(child.tag, dict(child.attrib), bounds, child_path, depth)
                elements.append(element)
                if parent is not None:
                    parent.children.append(element)
            walk(child, child_path, depth + 1, element or parent)

    walk(root, "", 0, None)
    return elements


def element_at(elements: list[UiElement], x: float, y: float) -> UiElement | None:
    """The most specific element under a point: smallest area, deepest on ties."""
    hits = [e for e in elements if e.contains(x, y) and e.area > 0]
    if not hits:
        return None
    return min(hits, key=lambda e: (e.area, -e.depth))


def clickable_target(element: UiElement, elements: list[UiElement]) -> UiElement:
    """For a non-clickable label inside a button, return the clickable ancestor if there is one."""
    if element.clickable:
        return element
    containing = [e for e in elements if e.clickable and e.depth < element.depth
                  and _inside(element.bounds, e.bounds)]
    return max(containing, key=lambda e: e.depth) if containing else element


def _inside(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _xpath_literal(value: str) -> str:
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ", '\"', ".join(f'"{p}"' for p in parts) + ")"


def _java_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def suggest_locators(element: UiElement, elements: list[UiElement]) -> list[LocatorSuggestion]:
    """Every useful way to find ``element``, across all locator types, most robust first.

    Unique locators come first; positional ones (which break when the layout
    shifts) are marked fragile and come last. The picker saves all unique ones on
    a step, so when one stops matching the next is tried.
    """
    def count(predicate) -> int:
        return sum(1 for e in elements if predicate(e))

    rid, desc, text, cls = element.resource_id, element.content_desc, element.text, element.class_name
    same = {
        "rid": lambda e: e.resource_id == rid,
        "desc": lambda e: e.content_desc == desc,
        "text": lambda e: e.text == text,
        "cls": lambda e: e.class_name == cls,
    }

    def both(*keys):
        return lambda e: all(same[k](e) for k in keys)

    suggestions: list[LocatorSuggestion] = []

    def add(locator_type: str, value: str, matches: int, fragile: bool = False) -> None:
        if all((s.locator_type, s.locator_value) != (locator_type, value) for s in suggestions):
            suggestions.append(LocatorSuggestion(locator_type, value, matches, fragile))

    # Single attributes, in each locator type that supports them.
    if rid:
        add("id", rid, count(same["rid"]))
    if desc:
        add("accessibility id", desc, count(same["desc"]))
    if text:
        add("text", text, count(same["text"]))
    if rid:
        add("android uiautomator", f"new UiSelector().resourceId({_java_string(rid)})", count(same["rid"]))
    if desc:
        add("android uiautomator", f"new UiSelector().description({_java_string(desc)})", count(same["desc"]))
    if text:
        add("android uiautomator", f"new UiSelector().text({_java_string(text)})", count(same["text"]))

    # Attribute combinations, for when one attribute alone isn't unique.
    labels = [(key, attr, value) for key, attr, value in
              (("rid", "resource-id", rid), ("desc", "content-desc", desc), ("text", "text", text)) if value]
    for i, (key_a, attr_a, value_a) in enumerate(labels):
        for key_b, attr_b, value_b in labels[i + 1:]:
            add("xpath", f"//*[@{attr_a}={_xpath_literal(value_a)} and @{attr_b}={_xpath_literal(value_b)}]",
                count(both(key_a, key_b)))
    for key, attr, value in labels:
        add("xpath", f"//{cls}[@{attr}={_xpath_literal(value)}]", count(both(key, "cls")))
        method = {"rid": "resourceId", "desc": "description", "text": "text"}[key]
        add("android uiautomator", f"new UiSelector().className({_java_string(cls)}).{method}({_java_string(value)})",
            count(both(key, "cls")))
    if count(same["cls"]) == 1:
        add("class name", cls, 1)

    anchored = _anchored_xpath(element, elements)
    if anchored:
        add(anchored.locator_type, anchored.locator_value, anchored.matches)

    # Position among look-alikes: unique by construction, but shifts if items are added above.
    if rid:
        same_id = [e for e in elements if same["rid"](e)]
        if len(same_id) > 1:
            position = same_id.index(element)
            add("xpath", f"(//*[@resource-id={_xpath_literal(rid)}])[{position + 1}]", 1)
            add("android uiautomator", f"new UiSelector().resourceId({_java_string(rid)}).instance({position})", 1)
    same_class = [e for e in elements if same["cls"](e)]
    add("android uiautomator",
        f"new UiSelector().className({_java_string(cls)}).instance({same_class.index(element)})", 1, fragile=True)
    add("xpath", element.xpath, 1, fragile=True)

    # Unique and stable first, keeping the robustness order within each group.
    return sorted(suggestions, key=lambda s: (not s.unique, s.fragile))


def best_locator(element: UiElement, elements: list[UiElement]) -> LocatorSuggestion:
    return suggest_locators(element, elements)[0]


def _anchored_xpath(element: UiElement, elements: list[UiElement]) -> LocatorSuggestion | None:
    """For an element with no id/text of its own, find it through a labelled child.

    Apps like Facebook Lite draw tabs and buttons as plain ViewGroups whose only
    identifying detail is a child's text or content-desc, e.g. the Video tab is
    the parent of ``content-desc="Video"``. ``//*[@content-desc="Video"]/..``
    survives scrolling and new feed items, unlike a positional path.
    """
    if element.resource_id or element.content_desc or element.text:
        return None
    descendants: list[UiElement] = []
    pending = list(element.children)
    while pending:
        node = pending.pop(0)  # breadth-first: the nearest label wins
        descendants.append(node)
        pending.extend(node.children)
    for node in descendants:
        for attr, value in (("content-desc", node.content_desc), ("text", node.text)):
            if not value:
                continue
            if sum(1 for e in elements if e.attrs.get(attr) == value) != 1:
                continue
            ups = "/.." * (node.depth - element.depth)
            return LocatorSuggestion("xpath", f"//*[@{attr}={_xpath_literal(value)}]{ups}", 1)
    return None
