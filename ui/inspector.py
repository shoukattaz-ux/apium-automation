"""Element picker / recorder: click on a live screenshot to get locators and build steps.

* Hover to see what's under the pointer, click to select it.
* The best locator is suggested automatically (unique ones first).
* "Add ..." buttons append steps to the Script Builder that opened the picker.
* **Record clicks** also performs each click on the phone and appends a Click
  step, then refreshes the screenshot — build a script by just using the app.
* In pick mode (opened from a step's "Pick…" button) it returns one locator.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import paths
from core.devices import DeviceError
from core.inspector import UiElement, clickable_target, element_at, parse_page_source, suggest_locators
from core.targeting import fingerprint, screen_signature

from .qtutil import safe_emit
from .theme import ACCENT

if TYPE_CHECKING:
    from .dashboard import MainWindow

ATTRIBUTES = ("text", "resource-id", "content-desc", "class", "package", "clickable", "enabled",
              "checked", "scrollable", "bounds")


class _Signals(QObject):
    loaded = Signal(bytes, str, str)   # png, page source, error
    clicked = Signal(str)              # error ('' on success)


class ScreenView(QWidget):
    """Draws the screenshot scaled to fit, with hover/selection outlines."""

    hovered = Signal(float, float)
    clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(260, 420)
        self.setCursor(Qt.CrossCursor)
        self.pixmap: QPixmap | None = None
        self.hover_rect: tuple[int, int, int, int] | None = None
        self.selected_rect: tuple[int, int, int, int] | None = None
        self.message = "Choose a phone and press Refresh"

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self.pixmap = pixmap
        self.hover_rect = self.selected_rect = None
        self.update()

    def _target(self) -> QRectF:
        if not self.pixmap or self.pixmap.isNull():
            return QRectF()
        scale = min(self.width() / self.pixmap.width(), self.height() / self.pixmap.height())
        width, height = self.pixmap.width() * scale, self.pixmap.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _to_image(self, point: QPointF) -> tuple[float, float] | None:
        target = self._target()
        if target.isEmpty() or not target.contains(point):
            return None
        scale = self.pixmap.width() / target.width()
        return (point.x() - target.x()) * scale, (point.y() - target.y()) * scale

    def _to_widget(self, rect: tuple[int, int, int, int]) -> QRectF:
        target = self._target()
        scale = target.width() / self.pixmap.width()
        left, top, right, bottom = rect
        return QRectF(target.x() + left * scale, target.y() + top * scale,
                      (right - left) * scale, (bottom - top) * scale)

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.pixmap or self.pixmap.isNull():
            painter.setPen(QColor("#9aa1ad"))
            painter.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        painter.drawPixmap(self._target(), self.pixmap, QRectF(self.pixmap.rect()))
        if self.hover_rect:
            pen = QPen(QColor("#f59e0b"), 1.5, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._to_widget(self.hover_rect))
        if self.selected_rect:
            painter.setPen(QPen(QColor(ACCENT), 2.5))
            fill = QColor(ACCENT)
            fill.setAlpha(40)
            painter.setBrush(fill)
            painter.drawRect(self._to_widget(self.selected_rect))

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        point = self._to_image(event.position())
        if point:
            self.hovered.emit(*point)

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        point = self._to_image(event.position())
        if point and event.button() == Qt.LeftButton:
            self.clicked.emit(*point)


class InspectorWindow(QWidget):
    """The element picker. ``add_steps`` receives steps for the builder; ``on_pick`` a locator."""

    def __init__(self, dashboard: "MainWindow", *, add_steps: Callable[[list[dict]], None] | None = None,
                 on_pick: Callable[[str, str], None] | None = None, roles: list[str] | None = None,
                 serial: str | None = None):
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle("Pick Element" if on_pick else "Element Picker & Recorder")
        if paths.icon_path().exists():
            self.setWindowIcon(QIcon(str(paths.icon_path())))
        self.resize(1080, 760)
        self.dashboard = dashboard
        self.add_steps = add_steps
        self.on_pick = on_pick
        self.elements: list[UiElement] = []
        self.selected: UiElement | None = None
        self.suggestions: list = []
        self._busy = False
        self.signals = _Signals(self)  # parented: dropped with the window if a worker finishes late
        self.signals.loaded.connect(self._show_screen)
        self.signals.clicked.connect(self._after_record_click)

        # ---- top bar
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(240)
        for device_serial, name in dashboard.connected_devices():
            self.device_combo.addItem(f"{name} ({device_serial})", device_serial)
        if serial:
            index = self.device_combo.findData(serial)
            if index >= 0:
                self.device_combo.setCurrentIndex(index)
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(self.refresh)
        self.record = QCheckBox("Record clicks")
        self.record.setToolTip("Clicking the screenshot also clicks on the phone and adds a Click step")
        self.record.setVisible(add_steps is not None)
        self.role_combo = QComboBox()
        self.role_combo.setEditable(True)
        self.role_combo.addItem("")
        self.role_combo.addItems(roles or [])
        self.role_combo.setToolTip("Phone role for added steps (leave empty for single-phone scripts)")
        self.role_combo.setMinimumWidth(70)
        role_label = QLabel("Phone role")
        role_label.setVisible(add_steps is not None)
        self.role_combo.setVisible(add_steps is not None)
        self.status = QLabel("")
        self.status.setObjectName("Muted")

        top = QHBoxLayout()
        top.addWidget(QLabel("Phone"))
        top.addWidget(self.device_combo)
        top.addWidget(refresh)
        top.addSpacing(12)
        top.addWidget(self.record)
        top.addWidget(role_label)
        top.addWidget(self.role_combo)
        top.addStretch(1)
        top.addWidget(self.status)

        # ---- screen
        self.screen = ScreenView()
        self.screen.hovered.connect(self._hover)
        self.screen.clicked.connect(self._click)
        screen_panel = QFrame()
        screen_panel.setObjectName("Panel")
        screen_layout = QVBoxLayout(screen_panel)
        screen_layout.setContentsMargins(10, 10, 10, 10)
        screen_layout.addWidget(self.screen)
        self.hover_label = QLabel(" ")
        self.hover_label.setObjectName("Muted")
        screen_layout.addWidget(self.hover_label)

        # ---- details
        self.title = QLabel("Nothing selected")
        self.title.setObjectName("PanelTitle")
        self.title.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Attribute", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.locators = QListWidget()
        self.locators.setMaximumHeight(150)

        actions = QVBoxLayout()
        if on_pick:
            use = QPushButton("Use these locators")
            use.setToolTip("The highlighted locator becomes the main one; the other unique ones are saved "
                           "as backups, tried in order if it doesn't match")
            use.setObjectName("Primary")
            use.clicked.connect(self._use_locator)
            actions.addWidget(use)
        if add_steps:
            grid = QHBoxLayout()
            for label, action in (("Click", "click"), ("Copy Text", "copy_text"),
                                  ("Type Text", "paste_text"), ("Wait For", "wait_for_element"),
                                  ("If Exists", "if_exists")):
                button = QPushButton(f"＋ {label}")
                button.clicked.connect(lambda _=False, a=action: self._add_step(a))
                grid.addWidget(button)
            actions.addLayout(grid)
        copy = QPushButton("Copy locator to clipboard")
        copy.clicked.connect(self._copy_locator)
        actions.addWidget(copy)

        details = QFrame()
        details.setObjectName("Panel")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(14, 14, 14, 14)
        details_layout.addWidget(self.title)
        details_layout.addWidget(self.table, 1)
        locator_title = QLabel("Locators (best first)")
        locator_title.setObjectName("Muted")
        details_layout.addWidget(locator_title)
        details_layout.addWidget(self.locators)
        details_layout.addLayout(actions)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(screen_panel)
        splitter.addWidget(details)
        splitter.setSizes([480, 600])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)

        if self.device_combo.count():
            QTimer.singleShot(0, self, self.refresh)  # context object: cancelled if the window closes
        else:
            self.screen.message = "No phones connected."

    # ------------------------------------------------------------------ loading

    def serial(self) -> str | None:
        return self.device_combo.currentData()

    def refresh(self) -> None:
        serial = self.serial()
        if not serial or self._busy:
            return
        if self.dashboard.manager.is_running(serial):
            self.status.setText("This phone is running a script — stop it to inspect.")
            return
        self._busy = True
        self.status.setText("Reading screen…")
        manager = self.dashboard.manager

        def work() -> None:
            try:
                session = manager.session_for(serial)
                png = session.screenshot_png()
                source = session.page_source()
                safe_emit(self.signals.loaded, png, source, "")
            except Exception as exc:  # DeviceError or a WebDriver error
                safe_emit(self.signals.loaded, b"", "", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)

        threading.Thread(target=work, name="inspector", daemon=True).start()

    def _show_screen(self, png: bytes, source: str, error: str) -> None:
        self._busy = False
        if error:
            self.status.setText(f"✖ {error}")
            return
        pixmap = QPixmap()
        pixmap.loadFromData(png, "PNG")
        try:
            self.elements = parse_page_source(source)
        except ValueError as exc:
            self.elements = []
            self.status.setText(str(exc))
        self.screen.set_pixmap(pixmap)
        self.selected = None
        self._show_details(None)
        if not error and self.elements:
            self.status.setText(f"{len(self.elements)} elements — click one to select it")

    # ------------------------------------------------------------------ selection

    def _hover(self, x: float, y: float) -> None:
        element = element_at(self.elements, x, y)
        self.screen.hover_rect = element.bounds if element else None
        self.hover_label.setText(element.label() if element else " ")
        self.screen.update()

    def _click(self, x: float, y: float) -> None:
        element = element_at(self.elements, x, y)
        if element is None:
            return
        self.select(element)
        if self.record.isChecked() and self.add_steps:
            self._record_click(element, x, y)

    def select(self, element: UiElement | None) -> None:
        self.selected = element
        self.screen.selected_rect = element.bounds if element else None
        self.screen.update()
        self._show_details(element)

    def _show_details(self, element: UiElement | None) -> None:
        self.table.setRowCount(0)
        self.locators.clear()
        self.suggestions = []
        if element is None:
            self.title.setText("Nothing selected")
            return
        self.title.setText(element.label())
        for name in ATTRIBUTES:
            value = element.attrs.get(name, "")
            if value:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(name))
                self.table.setItem(row, 1, QTableWidgetItem(value))
        self.suggestions = suggest_locators(element, self.elements)
        for suggestion in self.suggestions:
            badge = "✓ unique" if suggestion.unique else f"matches {suggestion.matches}"
            if suggestion.fragile:
                badge += " · fragile: breaks if the screen scrolls or changes"
            item = QListWidgetItem(f"{suggestion.locator_type}  =  {suggestion.locator_value}     [{badge}]")
            item.setData(Qt.UserRole, (suggestion.locator_type, suggestion.locator_value))
            self.locators.addItem(item)
        self.locators.setCurrentRow(0)

    def current_locator(self) -> tuple[str, str] | None:
        item = self.locators.currentItem()
        return tuple(item.data(Qt.UserRole)) if item else None

    # ------------------------------------------------------------------ actions

    def _role(self) -> str:
        return self.role_combo.currentText().strip()

    def ranked_locators(self) -> list[tuple[str, str]]:
        """Every unique locator, stable before fragile, the highlighted one first within its group.

        A fragile (positional) locator is never made the main one while a stable one exists,
        even if it was highlighted: it would fail as soon as the screen scrolls or changes.
        """
        chosen = self.current_locator()
        unique = [s for s in self.suggestions if s.unique]
        if not unique and chosen:
            return [chosen]
        unique.sort(key=lambda s: (s.fragile, (s.locator_type, s.locator_value) != chosen))
        return [(s.locator_type, s.locator_value) for s in unique]

    def _step_for(self, action: str) -> dict:
        """A step for the selected element: best locator as the main one, the rest as backups."""
        ranked = self.ranked_locators()
        locator, backups = ranked[0], ranked[1:]
        step: dict = {"action": action, "locator_type": locator[0], "locator_value": locator[1]}
        if backups:
            step["alternatives"] = [{"locator_type": t, "locator_value": v} for t, v in backups]
        if action == "click":
            step.update(self._fallback_position())
        step.update(self._safety_checks())
        if action == "copy_text":
            step["save_as"] = "copied_value"
        elif action == "paste_text":
            step["value_from"] = "copied_value"
        elif action == "wait_for_element":
            step["timeout_seconds"] = 15
        elif action == "if_exists":
            step["timeout_seconds"] = 3
            step["then"] = [{"action": "click", **{k: v for k, v in step.items() if k.startswith("locator")}}]
            step["else"] = []
        if self._role():
            step["device"] = self._role()
            for child in step.get("then", []):
                child["device"] = self._role()
        return step

    def _safety_checks(self) -> dict:
        """Fingerprint of the selected element and the screen it's on, checked before acting at run time."""
        if self.selected is None or not self.elements:
            return {}
        return {"target": fingerprint(self.selected, self.elements),
                "screen": screen_signature(self.selected, self.elements)}

    def _fallback_position(self) -> dict:
        """The selected element's centre as percent of the screen, as a Click step's backup tap."""
        if self.selected is None or not self.elements:
            return {}
        # Measure against the layout's own extent (what element bounds are in), not the screenshot's pixels.
        width = max(e.bounds[2] for e in self.elements)
        height = max(e.bounds[3] for e in self.elements)
        if width <= 0 or height <= 0:
            return {}
        left, top, right, bottom = self.selected.bounds
        return {"fallback_x": round(min(100.0, max(0.0, (left + right) / 2 / width * 100)), 1),
                "fallback_y": round(min(100.0, max(0.0, (top + bottom) / 2 / height * 100)), 1)}

    def _add_step(self, action: str) -> None:
        if not self.current_locator() or not self.add_steps:
            return
        self.add_steps([self._step_for(action)])
        self.status.setText(f"Added {action.replace('_', ' ')} step")

    def _use_locator(self) -> None:
        locators = self.ranked_locators()
        if locators and self.on_pick:
            position = self._fallback_position()
            self.on_pick({"locators": locators,
                          "position": (position["fallback_x"], position["fallback_y"]) if position else None,
                          **self._safety_checks()})
            self.close()

    def _copy_locator(self) -> None:
        locator = self.current_locator()
        if locator:
            QGuiApplication.clipboard().setText(locator[1])
            self.status.setText("Locator copied")

    def _record_click(self, element: UiElement, x: float, y: float) -> None:
        target = clickable_target(element, self.elements)
        if target is not element:
            self.select(target)
        locator = self.current_locator()
        if not locator:
            return
        self.add_steps([self._step_for("click")])
        serial = self.serial()
        manager = self.dashboard.manager
        self.status.setText("Clicking on the phone…")

        def work() -> None:
            try:
                session = manager.session_for(serial)
                try:
                    session.click(*locator, timeout_seconds=3)
                except DeviceError:
                    session.tap(x, y)
                safe_emit(self.signals.clicked, "")
            except Exception as exc:
                safe_emit(self.signals.clicked, str(exc).splitlines()[0] if str(exc) else type(exc).__name__)

        threading.Thread(target=work, name="inspector-click", daemon=True).start()

    def _after_record_click(self, error: str) -> None:
        if error:
            self.status.setText(f"✖ Click failed: {error}")
            return
        self.status.setText("Recorded click — refreshing…")
        QTimer.singleShot(1200, self, self.refresh)
