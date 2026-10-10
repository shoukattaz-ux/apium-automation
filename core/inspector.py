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


def suggest_locators(element: UiElement, elements: list[UiElement]) -> list[LocatorSuggestion]:
    """Locators for ``element``, most robust first. Unique ones are preferred by callers."""
    def count(predicate) -> int:
        return sum(1 for e in elements if predicate(e))

    suggestions: list[LocatorSuggestion] = []
    if element.resource_id:
        suggestions.append(LocatorSuggestion("id", element.resource_id,
                                             count(lambda e: e.resource_id == element.resource_id)))
    if element.content_desc:
        suggestions.append(LocatorSuggestion("accessibility id", element.content_desc,
                                             count(lambda e: e.content_desc == element.content_desc)))
    if element.text:
        suggestions.append(LocatorSuggestion("text", element.text, count(lambda e: e.text == element.text)))
    if element.resource_id and element.text:
        xpath = (f"//*[@resource-id={_xpath_literal(element.resource_id)}"
                 f" and @text={_xpath_literal(element.text)}]")
        suggestions.append(LocatorSuggestion("xpath", xpath, count(
            lambda e: e.resource_id == element.resource_id and e.text == element.text)))
    if element.resource_id:
        same_id = [e for e in elements if e.resource_id == element.resource_id]
        if len(same_id) > 1:
            position = same_id.index(element) + 1
            xpath = f"(//*[@resource-id={_xpath_literal(element.resource_id)}])[{position}]"
            suggestions.append(LocatorSuggestion("xpath", xpath, 1))
    anchored = _anchored_xpath(element, elements)
    if anchored:
        suggestions.append(anchored)
    suggestions.append(LocatorSuggestion("xpath", element.xpath, 1, fragile=True))

    # Unique and stable first, keeping the robustness order within each group.
    return sorted(suggestions, key=lambda s: (not s.unique, s.fragile))


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


def best_locator(element: UiElement, elements: list[UiElement]) -> LocatorSuggestion:
    return suggest_locators(element, elements)[0]
