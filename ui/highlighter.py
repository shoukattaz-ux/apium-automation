"""Minimal Python syntax highlighting for the Developer Mode editor."""

from __future__ import annotations

import keyword
import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Bold)
    fmt.setFontItalic(italic)
    return fmt


KEYWORD = _fmt("#c792ea", bold=True)
BUILTIN = _fmt("#82aaff")
DEVICE_API = _fmt("#7fdbca")
STRING = _fmt("#c3e88d")
COMMENT = _fmt("#697098", italic=True)
NUMBER = _fmt("#f78c6c")
DEFINITION = _fmt("#ffcb6b", bold=True)

_DEVICE_METHODS = ("open_app", "click", "scroll", "wait", "wait_for_element", "copy_text",
                   "paste_text", "tap", "back", "current_package", "screenshot", "installed_packages")


class PythonHighlighter(QSyntaxHighlighter):
    _rules = [
        (re.compile(r"\b(" + "|".join(keyword.kwlist) + r")\b"), KEYWORD),
        (re.compile(r"\b(print|len|range|str|int|float|list|dict|set|tuple|enumerate|"
                    r"isinstance|min|max|sum|sorted|open|log|variables|stop_requested)\b"), BUILTIN),
        (re.compile(r"\.(" + "|".join(_DEVICE_METHODS) + r")\b"), DEVICE_API),
        (re.compile(r"\b\d+(\.\d+)?\b"), NUMBER),
        (re.compile(r"(?<=\bdef\s)\w+|(?<=\bclass\s)\w+"), DEFINITION),
        (re.compile(r"'[^'\\\n]*(\\.[^'\\\n]*)*'|\"[^\"\\\n]*(\\.[^\"\\\n]*)*\""), STRING),
        (re.compile(r"#[^\n]*"), COMMENT),
    ]
    _triple = re.compile(r'"""|\'\'\'')

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        for pattern, fmt in self._rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)
        self._highlight_triple_quotes(text)

    def _highlight_triple_quotes(self, text: str) -> None:
        # Block state 1 = inside a triple-quoted string carried over from the previous line.
        inside = self.previousBlockState() == 1
        start = 0
        position = 0
        while True:
            match = self._triple.search(text, position)
            if match is None:
                break
            if inside:
                self.setFormat(start, match.end() - start, STRING)
                inside = False
            else:
                start = match.start()
                inside = True
            position = match.end()
        if inside:
            self.setFormat(start, len(text) - start, STRING)
        self.setCurrentBlockState(1 if inside else 0)
