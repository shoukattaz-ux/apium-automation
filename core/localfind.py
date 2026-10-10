"""Evaluating locators against one snapshot of the screen layout.

Asking the phone about each of a step's locators separately costs several round trips
per locator. Instead the runner reads the screen's layout once per look (the same XML
UiAutomator2 gives the element picker) and evaluates every locator against it here,
which also gives each match's full attributes for the fingerprint check at no extra
cost. Locators this module can't evaluate (UiSelector methods it doesn't know) return
None and the session asks the phone instead.
"""

from __future__ import annotations

import re

from lxml import etree

ATTRS = ("class", "resource-id", "text", "content-desc", "bounds", "displayed", "package")
_UISELECTOR_CALL = re.compile(r'\.(\w+)\(\s*(?:"((?:[^"\\]|\\.)*)"|(-?\d+)|(true|false))\s*\)')
_UISELECTOR_EXACT = {"resourceId": "resource-id", "description": "content-desc", "text": "text",
                     "className": "class"}


class LocalScreen:
    """One snapshot of the screen layout that locators can be evaluated against."""

    def __init__(self, page_source: str):
        data = page_source.encode("utf-8") if isinstance(page_source, str) else page_source
        self.root = etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True))
        # Document-order list of on-screen nodes (those with bounds), like UiAutomator traverses them.
        self.nodes = [node for node in self.root.iter() if isinstance(node.tag, str) and node.get("bounds")]

    @staticmethod
    def attrs(node) -> dict[str, str]:
        values = {name: node.get(name, "") for name in ATTRS}
        values["class"] = values["class"] or node.tag
        return values

    def _visible(self, nodes) -> list[dict[str, str]]:
        return [self.attrs(n) for n in nodes if n.get("bounds") and n.get("displayed", "true") != "false"]

    def evaluate(self, locator_type: str, locator_value: str) -> list[dict[str, str]] | None:
        """Attributes of every element the locator matches, or None if it must be asked of the phone."""
        kind = locator_type.strip().lower()
        if kind == "id":
            if ":" in locator_value:
                matches = [n for n in self.nodes if n.get("resource-id") == locator_value]
            else:  # Appium accepts a bare id and adds the app's package
                matches = [n for n in self.nodes if (n.get("resource-id") or "").endswith(":id/" + locator_value)
                           or n.get("resource-id") == locator_value]
            return self._visible(matches)
        if kind == "accessibility id":
            return self._visible(n for n in self.nodes if n.get("content-desc") == locator_value)
        if kind == "text":
            return self._visible(n for n in self.nodes if n.get("text") == locator_value)
        if kind == "class name":
            return self._visible(n for n in self.nodes if (n.get("class") or n.tag) == locator_value)
        if kind == "xpath":
            try:
                found = self.root.xpath(locator_value)
            except (etree.XPathError, etree.XPathEvalError) as exc:
                raise ValueError(f"invalid selector: {exc}") from exc
            if not isinstance(found, list):
                raise ValueError("invalid selector: the XPath doesn't select elements")
            return self._visible(n for n in found if isinstance(n, etree._Element))
        if kind == "android uiautomator":
            return self._uiselector(locator_value)
        return None

    def _uiselector(self, expression: str) -> list[dict[str, str]] | None:
        """``new UiSelector().className("x").text("y").instance(2)`` and friends; None if unsupported."""
        expression = expression.strip().rstrip(";")
        if not expression.startswith("new UiSelector()"):
            return None
        rest = expression[len("new UiSelector()"):]
        calls = _UISELECTOR_CALL.findall(rest)
        if _UISELECTOR_CALL.sub("", rest).strip():
            return None  # something we didn't understand: let the phone evaluate it
        matches = list(self.nodes)
        instance: int | None = None
        for method, text, number, _flag in calls:
            text = text.replace('\\"', '"').replace("\\\\", "\\")
            if method in _UISELECTOR_EXACT:
                attr = _UISELECTOR_EXACT[method]
                matches = [n for n in matches if (n.get(attr) or (n.tag if attr == "class" else "")) == text]
            elif method == "instance" and number:
                instance = int(number)
            else:
                return None
        visible = self._visible(matches)
        if instance is not None:
            return visible[instance:instance + 1]
        return visible
