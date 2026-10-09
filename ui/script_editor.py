"""The New Script window.

Two tabs share one window:

* **Script Builder** — non-technical users add steps through forms; the result
  is saved as ``configs/<name>.json`` without anyone seeing JSON. Supports
  Repeat / If-exists blocks with nested steps, a phone role per step for
  cross-phone workflows, retries and stop-on-failure, ``{{variable}}``
  placeholders, and picking elements straight from a phone screenshot.
* **Developer Mode** — technical users write ``def run(device): ...`` in Python
  using the same DeviceSession action API, run it on a phone, and save it to
  ``configs/scripts/<name>.py``.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFontDatabase, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from core import paths
from core.devices import DeviceError, list_installed_packages
from core.schema import (
    ACTION_LABELS, ACTIONS, BLOCK_LABELS, COMMON_FIELDS, FieldSpec, ScriptError, describe_step, format_path,
    safe_filename, save_script, script_roles, script_variables, validate_script, validate_step,
)

from .highlighter import PythonHighlighter

if TYPE_CHECKING:
    from .dashboard import MainWindow

DEVELOPER_TEMPLATE = '''\
def run(device):
    """Called with the DeviceSession of the phone you pick.

    Actions: open_app, close_app, click, wait_for_element, exists,
    copy_text, paste_text, scroll, scroll_to_text, swipe_percent,
    tap_percent, press_key, wait, screenshot_png, current_package.
    Locator types: "id", "xpath", "accessibility id", "text", "class name".
    `variables` is a dict shared with the run; print() goes to the log.
    """
    device.open_app("com.example.app")
    device.wait_for_element("id", "com.example.app:id/title", timeout_seconds=15)
    value = device.copy_text("id", "com.example.app:id/title")
    print("Copied:", value)
    if device.exists("text", "Next", timeout_seconds=3):
        device.click("text", "Next")
    device.scroll("down", times=2)
'''

PickLocator = Callable[[Callable[[str, str], None]], None]


# ======================================================================= helpers


class _PackagesLoaded(QObject):
    done = Signal(list, str)


class PackagePickerDialog(QDialog):
    """Lists apps installed on a connected phone so users needn't know package IDs."""

    def __init__(self, devices: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick an app from a connected device")
        self.resize(460, 520)
        self.devices = devices
        self._signals = _PackagesLoaded()
        self._signals.done.connect(self._show_packages)
        self._packages: list[str] = []

        self.device_combo = QComboBox()
        for serial, name in devices:
            self.device_combo.addItem(f"{name} ({serial})", serial)
        self.system_apps = QCheckBox("Include system apps")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter, e.g. whatsapp")
        self.list = QListWidget()
        self.message = QLabel("")
        self.message.setObjectName("Muted")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())

        layout = QVBoxLayout(self)
        layout.addWidget(self.device_combo)
        layout.addWidget(self.system_apps)
        layout.addWidget(self.filter)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.message)
        layout.addWidget(buttons)

        self.device_combo.currentIndexChanged.connect(self.load)
        self.system_apps.toggled.connect(self.load)
        self.filter.textChanged.connect(self._apply_filter)
        if devices:
            self.load()
        else:
            self.message.setText("No phones connected.")

    def load(self) -> None:
        serial = self.device_combo.currentData()
        if not serial:
            return
        self.message.setText("Loading installed apps…")
        self.list.clear()
        third_party_only = not self.system_apps.isChecked()

        def work() -> None:
            try:
                self._signals.done.emit(list_installed_packages(serial, third_party_only), "")
            except DeviceError as exc:
                self._signals.done.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _show_packages(self, packages: list, error: str) -> None:
        self._packages = packages
        self.message.setText(error or f"{len(packages)} apps")
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.filter.text().strip().lower()
        self.list.clear()
        self.list.addItems([p for p in self._packages if needle in p.lower()])
        if self.list.count():
            self.list.setCurrentRow(0)

    def selected_package(self) -> str | None:
        item = self.list.currentItem()
        return item.text() if item else None


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class StepDialog(QDialog):
    """Add/edit one step. Fields are generated from ``core.schema.ACTIONS``.

    Nested steps of a Repeat / If block are kept when the block is edited.
    """

    def __init__(self, parent=None, step: dict | None = None, variables: list[str] | None = None,
                 devices: list[tuple[str, str]] | None = None, roles: list[str] | None = None,
                 pick_locator: PickLocator | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Step" if step else "Add Step")
        self.setMinimumWidth(620)
        self.original = step or {}
        self.variables = variables or []
        self.devices = devices or []
        self.roles = roles or []
        self.pick_locator = pick_locator
        self.inputs: dict[str, QWidget] = {}
        self.errors: dict[str, QLabel] = {}
        self.result_step: dict | None = None

        self.action_combo = QComboBox()
        group = None
        for key, spec in ACTIONS.items():
            if spec.group != group:
                if group is not None:
                    self.action_combo.insertSeparator(self.action_combo.count())
                group = spec.group
            self.action_combo.addItem(f"{spec.label}", key)
        self.action_help = QLabel()
        self.action_help.setObjectName("Muted")
        self.action_error = QLabel()
        self.action_error.setObjectName("Error")
        self.action_error.hide()

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 8, 0, 0)
        self.form.setVerticalSpacing(4)
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.advanced = QGroupBox("Phone and failure handling")
        self.advanced_form = QFormLayout(self.advanced)
        self.advanced_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Save Step")
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        top = QFormLayout()
        top.addRow("Action", self.action_combo)
        top.addRow("", self.action_help)
        top.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.action_error)
        layout.addWidget(self.form_host)
        layout.addWidget(self.advanced)
        layout.addStretch(1)
        layout.addWidget(buttons)

        if step:
            index = self.action_combo.findData(step.get("action"))
            self.action_combo.setCurrentIndex(max(0, index))
        self.action_combo.currentIndexChanged.connect(lambda _: self._build_fields())
        self._build_fields(step)

    def action(self) -> str:
        return self.action_combo.currentData()

    def _clear(self, form: QFormLayout) -> None:
        while form.rowCount():
            form.removeRow(0)

    def _build_fields(self, step: dict | None = None) -> None:
        self._clear(self.form)
        self._clear(self.advanced_form)
        self.inputs.clear()
        self.errors.clear()
        self.action_error.hide()
        spec = ACTIONS[self.action()]
        self.action_help.setText(spec.description)
        for field_spec in spec.fields:
            self._add_field(self.form, field_spec, step)
        self.advanced.setVisible(spec.uses_device)
        if spec.uses_device:
            for field_spec in COMMON_FIELDS:
                self._add_field(self.advanced_form, field_spec, step)
        self.adjustSize()

    def _add_field(self, form: QFormLayout, spec: FieldSpec, step: dict | None) -> None:
        value = step.get(spec.name, spec.default) if step else spec.default
        widget = self._make_input(spec, value)
        self.inputs[spec.name] = widget
        error = QLabel()
        error.setObjectName("Error")
        error.hide()
        self.errors[spec.name] = error
        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 6)
        column.setSpacing(2)
        row = QHBoxLayout()
        row.addWidget(widget, 1)
        if spec.name == "package":
            detect = QPushButton("Detect from phone…")
            detect.setEnabled(bool(self.devices))
            detect.setToolTip("" if self.devices else "Connect a phone first")
            detect.clicked.connect(self._detect_package)
            row.addWidget(detect)
        if spec.name == "locator_value" and self.pick_locator:
            pick = QPushButton("Pick from screen…")
            pick.setToolTip("Click the element on a live screenshot of the phone")
            pick.clicked.connect(self._pick)
            row.addWidget(pick)
        column.addLayout(row)
        if spec.help:
            hint = QLabel(spec.help)
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            column.addWidget(hint)
        column.addWidget(error)
        form.addRow(spec.label + ("" if spec.required else " (optional)"), container)

    def _make_input(self, spec: FieldSpec, value: Any) -> QWidget:
        if spec.kind in ("choice", "locator_type"):
            combo = QComboBox()
            combo.addItems(list(spec.choices))
            if value in spec.choices:
                combo.setCurrentText(value)
            return combo
        if spec.kind == "int":
            box = QSpinBox()
            box.setRange(int(spec.minimum or 0), int(spec.maximum if spec.maximum is not None else 100000))
            box.setValue(int(value) if value not in (None, "") else int(spec.minimum or 0))
            return box
        if spec.kind == "float":
            box = QDoubleSpinBox()
            box.setRange(float(spec.minimum or 0), float(spec.maximum if spec.maximum is not None else 3600))
            box.setDecimals(1)
            box.setSingleStep(0.5)
            if "seconds" in spec.name or spec.name == "seconds":
                box.setSuffix(" s")
            elif "%" in spec.label:
                box.setSuffix(" %")
            box.setValue(float(value) if value not in (None, "") else 0.0)
            return box
        if spec.name in ("value_from", "device"):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")
            combo.addItems(self.variables if spec.name == "value_from" else self.roles)
            combo.setCurrentText("" if value is None else str(value))
            if spec.name == "device":
                combo.lineEdit().setPlaceholderText("default phone")
            return combo
        line = QLineEdit("" if value is None else str(value))
        if spec.templated:
            line.setPlaceholderText("{{variable}} placeholders allowed")
        return line

    def _value(self, widget: QWidget) -> Any:
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        return widget.text().strip()

    def _detect_package(self) -> None:
        dialog = PackagePickerDialog(self.devices, self)
        if dialog.exec() == QDialog.Accepted and dialog.selected_package():
            self.inputs["package"].setText(dialog.selected_package())

    def _pick(self) -> None:
        def apply(locator_type: str, locator_value: str) -> None:
            self.inputs["locator_type"].setCurrentText(locator_type)
            self.inputs["locator_value"].setText(locator_value)
            self.raise_()
            self.activateWindow()
        self.pick_locator(apply)

    def current_step(self) -> dict:
        step: dict[str, Any] = {"action": self.action()}
        for name, widget in self.inputs.items():
            value = self._value(widget)
            if value != "":
                step[name] = value
        for key in ACTIONS[self.action()].blocks:
            step[key] = copy.deepcopy(self.original.get(key, []))
        return step

    def _accept(self) -> None:
        step = self.current_step()
        problems = validate_step(step)
        for name, label in self.errors.items():
            message = problems.get(name)
            label.setText(message or "")
            label.setVisible(bool(message))
            widget = self.inputs[name]
            widget.setProperty("invalid", "true" if message else "false")
            _repolish(widget)
        self.action_error.setText(problems.get("action", ""))
        self.action_error.setVisible("action" in problems)
        if problems:
            return
        self.result_step = step
        self.accept()


# ======================================================================= step cards


class StepCard(QFrame):
    """One step; Repeat / If cards contain their nested step lists."""

    def __init__(self, builder: "BuilderTab", steps: list[dict], index: int, path: tuple[int, ...], depth: int):
        super().__init__()
        self.setObjectName("StepCard")
        self.setProperty("depth", str(depth % 3))
        step = steps[index]
        spec = ACTIONS.get(step.get("action"))

        number = QLabel(format_path(path))
        number.setObjectName("Muted")
        number.setMinimumWidth(28)
        number.setAlignment(Qt.AlignCenter)
        title = QLabel(ACTION_LABELS.get(step.get("action"), step.get("action", "?")))
        title.setObjectName("DeviceName")
        summary = QLabel(describe_step(step))
        summary.setObjectName("Muted")
        summary.setWordWrap(True)
        header_text = QVBoxLayout()
        header_text.setSpacing(1)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.addWidget(title)
        if step.get("device"):
            badge = QLabel(f"Phone {step['device']}")
            badge.setObjectName("Badge")
            title_row.addWidget(badge)
        title_row.addStretch(1)
        header_text.addLayout(title_row)
        header_text.addWidget(summary)

        def button(label: str, tip: str, slot, enabled: bool = True, name: str = "Icon") -> QPushButton:
            b = QPushButton(label)
            b.setObjectName(name)
            b.setToolTip(tip)
            b.setEnabled(enabled)
            b.clicked.connect(slot)
            return b

        header = QHBoxLayout()
        header.addWidget(number)
        header.addLayout(header_text, 1)
        header.addWidget(button("▲", "Move up", lambda: builder.move_step(steps, index, index - 1), index > 0))
        header.addWidget(button("▼", "Move down", lambda: builder.move_step(steps, index, index + 1),
                                index < len(steps) - 1))
        header.addWidget(button("⧉", "Duplicate", lambda: builder.duplicate_step(steps, index)))
        header.addWidget(button("✎", "Edit step", lambda: builder.edit_step(steps, index)))
        header.addWidget(button("✕", "Delete step", lambda: builder.delete_step(steps, index), name="Danger"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addLayout(header)

        for key in (spec.blocks if spec else ()):
            children = step.setdefault(key, [])
            block_title = QLabel(BLOCK_LABELS[key])
            block_title.setObjectName("BlockTitle")
            nested = QVBoxLayout()
            nested.setContentsMargins(26, 4, 0, 0)
            nested.setSpacing(6)
            nested.addWidget(block_title)
            builder.render_list(nested, children, path, depth + 1)
            layout.addLayout(nested)


# ======================================================================= tabs


class BuilderTab(QWidget):
    saved = Signal(Path)

    def __init__(self, dashboard: "MainWindow | None", parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.steps: list[dict] = []
        self.saved_path: Path | None = None

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Copy order number to sheet")
        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("What does this script do? (optional)")
        self.name_error = QLabel()
        self.name_error.setObjectName("Error")
        self.name_error.hide()
        self.mode_label = QLabel()
        self.mode_label.setObjectName("Muted")
        self.mode_label.setWordWrap(True)

        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.cards_host)

        self.steps_error = QLabel()
        self.steps_error.setObjectName("Error")
        self.steps_error.setWordWrap(True)
        self.steps_error.hide()
        add = QPushButton("＋  Add Step")
        add.clicked.connect(lambda: self.add_step(self.steps))
        pick = QPushButton("◎  Pick / Record from Screen")
        pick.setToolTip("Open the element picker: click elements on a phone screenshot to add steps")
        pick.clicked.connect(self.open_picker)
        pick.setEnabled(dashboard is not None)
        save = QPushButton("Save Script")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        save_run = QPushButton("Save && Run…")
        save_run.clicked.connect(self.save_and_run)
        save_run.setEnabled(dashboard is not None)
        self.saved_label = QLabel()
        self.saved_label.setObjectName("Muted")

        name_form = QFormLayout()
        name_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        name_form.addRow("Script name", self.name_edit)
        name_form.addRow("Description", self.description_edit)
        steps_header = QHBoxLayout()
        steps_title = QLabel("Steps")
        steps_title.setObjectName("PanelTitle")
        steps_header.addWidget(steps_title)
        steps_header.addWidget(self.mode_label, 1)
        steps_header.addWidget(pick)
        steps_header.addWidget(add)
        footer = QHBoxLayout()
        footer.addWidget(self.saved_label, 1)
        footer.addWidget(save_run)
        footer.addWidget(save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addLayout(name_form)
        layout.addWidget(self.name_error)
        layout.addLayout(steps_header)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.steps_error)
        layout.addLayout(footer)
        self.render()

    # ------------------------------------------------------------------ data

    def load(self, script: dict) -> None:
        self.name_edit.setText(script.get("name", ""))
        self.description_edit.setText(script.get("description", ""))
        self.steps = copy.deepcopy(script.get("steps", []))
        self.render()

    def script(self) -> dict:
        script = {"name": self.name_edit.text().strip(), "steps": self.steps}
        if self.description_edit.text().strip():
            script["description"] = self.description_edit.text().strip()
        return script

    def _devices(self) -> list[tuple[str, str]]:
        return self.dashboard.connected_devices() if self.dashboard else []

    def roles(self) -> list[str]:
        return script_roles({"steps": self.steps})

    def append_steps(self, steps: list[dict]) -> None:
        self.steps.extend(steps)
        self.render()

    # ------------------------------------------------------------------ rendering

    def render(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                _delete_layout(item.layout())
        if not self.steps:
            empty = QLabel("No steps yet. Click “Add Step”, or “Pick / Record from Screen” to build\n"
                           "the script by clicking on a live screenshot of your phone.")
            empty.setObjectName("EmptyState")
            empty.setAlignment(Qt.AlignCenter)
            self.cards_layout.addWidget(empty)
        else:
            self.render_list(self.cards_layout, self.steps, (), 0, add_button=False)
        self.cards_layout.addStretch(1)
        roles = self.roles()
        if len(roles) >= 2:
            self.mode_label.setText(f"Cross-phone workflow · phones {', '.join(roles)}")
        else:
            self.mode_label.setText("Single-phone script · runs on each phone you start it on")

    def render_list(self, layout: QVBoxLayout, steps: list[dict], path: tuple[int, ...], depth: int,
                    add_button: bool = True) -> None:
        for index in range(len(steps)):
            layout.addWidget(StepCard(self, steps, index, path + (index + 1,), depth))
        if add_button:
            add = QPushButton("＋ Add step here")
            add.setObjectName("Ghost")
            add.clicked.connect(lambda _=False, s=steps: self.add_step(s))
            row = QHBoxLayout()
            row.addWidget(add)
            row.addStretch(1)
            layout.addLayout(row)

    # ------------------------------------------------------------------ editing

    def _dialog(self, step: dict | None = None) -> StepDialog:
        return StepDialog(self, step=step, variables=script_variables({"steps": self.steps}),
                          devices=self._devices(), roles=self.roles() or ["A", "B"],
                          pick_locator=self._pick_locator if self.dashboard else None)

    def add_step(self, steps: list[dict]) -> None:
        dialog = self._dialog()
        if dialog.exec() == QDialog.Accepted and dialog.result_step:
            steps.append(dialog.result_step)
            self.render()

    def edit_step(self, steps: list[dict], index: int) -> None:
        dialog = self._dialog(steps[index])
        if dialog.exec() == QDialog.Accepted and dialog.result_step:
            steps[index] = dialog.result_step
            self.render()

    def delete_step(self, steps: list[dict], index: int) -> None:
        step = steps[index]
        nested = sum(len(step.get(k, [])) for k in ACTIONS.get(step.get("action")).blocks) \
            if step.get("action") in ACTIONS else 0
        if nested and QMessageBox.question(self, "Delete block?",
                                           f"This block contains {nested} step(s). Delete it all?") != QMessageBox.Yes:
            return
        del steps[index]
        self.render()

    def duplicate_step(self, steps: list[dict], index: int) -> None:
        steps.insert(index + 1, copy.deepcopy(steps[index]))
        self.render()

    def move_step(self, steps: list[dict], source: int, target: int) -> None:
        if 0 <= target < len(steps):
            steps.insert(target, steps.pop(source))
            self.render()

    # ------------------------------------------------------------------ picker

    def _pick_locator(self, apply: Callable[[str, str], None]) -> None:
        self.dashboard.open_inspector(on_pick=apply)

    def open_picker(self) -> None:
        if self.dashboard:
            self.dashboard.open_inspector(add_steps=self.append_steps, roles=self.roles())

    # ------------------------------------------------------------------ saving

    def save(self) -> Path | None:
        script = self.script()
        name = script["name"]
        self.name_error.setVisible(not name)
        self.name_error.setText("Give the script a name" if not name else "")
        problems = [p for p in validate_script(script) if p != "Script needs a name"]
        self.steps_error.setText("\n".join(problems))
        self.steps_error.setVisible(bool(problems))
        if not name or problems:
            return None
        target = paths.configs_dir() / f"{safe_filename(name)}.json"
        if target.exists() and target != self.saved_path:
            answer = QMessageBox.question(self, "Replace script?",
                                          f"A script called “{target.stem}” already exists. Replace it?")
            if answer != QMessageBox.Yes:
                return None
        try:
            path = save_script(script, paths.configs_dir())
        except (ScriptError, OSError) as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return None
        self.saved_path = path
        self.saved_label.setText(f"Saved to {path.name}")
        self.saved.emit(path)
        return path

    def save_and_run(self) -> None:
        path = self.save()
        if path and self.dashboard:
            self.dashboard.open_run_dialog(path)


def _delete_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            _delete_layout(item.layout())


class DeveloperTab(QWidget):
    saved = Signal(Path)

    def __init__(self, dashboard: "MainWindow | None", parent=None):
        super().__init__(parent)
        self.dashboard = dashboard

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. sync_orders")
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(220)
        refresh = QPushButton("⟳")
        refresh.setObjectName("Icon")
        refresh.setToolTip("Refresh device list")
        refresh.clicked.connect(self.refresh_devices)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Code")
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(" ") * 4)
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setPlainText(DEVELOPER_TEMPLATE)
        self.highlighter = PythonHighlighter(self.editor.document())

        self.message = QLabel("Output and errors appear in the dashboard log for the chosen device.")
        self.message.setObjectName("Muted")
        self.message.setWordWrap(True)
        run = QPushButton("▶  Run on this device")
        run.setObjectName("Primary")
        run.clicked.connect(self.run)
        stop = QPushButton("■  Stop")
        stop.setObjectName("Danger")
        stop.clicked.connect(self.stop)
        save = QPushButton("Save as Script")
        save.clicked.connect(self.save)
        QShortcut(QKeySequence("F5"), self, activated=self.run)

        top = QHBoxLayout()
        top.addWidget(QLabel("Name"))
        top.addWidget(self.name_edit, 1)
        top.addSpacing(12)
        top.addWidget(QLabel("Device"))
        top.addWidget(self.device_combo)
        top.addWidget(refresh)
        footer = QHBoxLayout()
        footer.addWidget(self.message, 1)
        footer.addWidget(stop)
        footer.addWidget(save)
        footer.addWidget(run)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.editor, 1)
        layout.addLayout(footer)
        self.refresh_devices()

    def load(self, path: Path) -> None:
        self.name_edit.setText(path.stem)
        self.editor.setPlainText(path.read_text(encoding="utf-8"))

    def refresh_devices(self) -> None:
        current = self.device_combo.currentData()
        self.device_combo.clear()
        for serial, name in (self.dashboard.connected_devices() if self.dashboard else []):
            self.device_combo.addItem(f"{name} ({serial})", serial)
        index = self.device_combo.findData(current)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)

    def _check_syntax(self) -> bool:
        try:
            compile(self.editor.toPlainText(), self.name_edit.text() or "<script>", "exec")
        except SyntaxError as exc:
            self.message.setText(f"Syntax error on line {exc.lineno}: {exc.msg}")
            self.message.setObjectName("Error")
            self.message.style().unpolish(self.message)
            self.message.style().polish(self.message)
            if exc.lineno:
                block = self.editor.document().findBlockByLineNumber(exc.lineno - 1)
                cursor = self.editor.textCursor()
                cursor.setPosition(block.position())
                self.editor.setTextCursor(cursor)
            return False
        self.message.setObjectName("Muted")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        return True

    def run(self) -> None:
        if not self._check_syntax():
            return
        serial = self.device_combo.currentData()
        if not serial:
            self.refresh_devices()
            serial = self.device_combo.currentData()
        if not serial or self.dashboard is None:
            QMessageBox.information(self, "No device", "Connect a phone and pick it in the Device list.")
            return
        if self.dashboard.run_python_source(serial, self.editor.toPlainText(), self.name_edit.text().strip()):
            self.message.setText(f"Running on {serial} — see the dashboard log.")

    def stop(self) -> None:
        serial = self.device_combo.currentData()
        if serial and self.dashboard:
            self.dashboard.manager.stop(serial)

    def save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.message.setText("Give the script a name before saving.")
            self.name_edit.setFocus()
            return
        if not self._check_syntax():
            return
        if "def run(" not in self.editor.toPlainText():
            self.message.setText("Developer scripts must define a function `run(device)`.")
            return
        folder = paths.python_scripts_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{safe_filename(name)}.py"
        if target.exists():
            answer = QMessageBox.question(self, "Replace script?",
                                          f"A developer script called “{target.stem}” already exists. Replace it?")
            if answer != QMessageBox.Yes:
                return
        target.write_text(self.editor.toPlainText(), encoding="utf-8")
        self.message.setText(f"Saved to scripts/{target.name}")
        self.saved.emit(target)


# ======================================================================= window


class ScriptEditorWindow(QWidget):
    """The 'New Script' window with Script Builder and Developer Mode tabs."""

    saved = Signal(Path)

    def __init__(self, dashboard: "MainWindow | None" = None, script_path: Path | None = None):
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle("New Script")
        if paths.icon_path().exists():
            self.setWindowIcon(QIcon(str(paths.icon_path())))
        self.resize(900, 760)

        self.builder = BuilderTab(dashboard)
        self.developer = DeveloperTab(dashboard)
        self.builder.saved.connect(self.saved)
        self.developer.saved.connect(self.saved)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.builder, "Script Builder")
        self.tabs.addTab(self.developer, "Developer Mode")
        self.tabs.currentChanged.connect(self._tab_changed)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_current)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.tabs)

        if script_path:
            self.open_path(Path(script_path))

    def _tab_changed(self, index: int) -> None:
        if index == 1:
            self.developer.refresh_devices()

    def save_current(self) -> None:
        if self.tabs.currentWidget() is self.developer:
            self.developer.save()
        else:
            self.builder.save()

    def open_path(self, path: Path) -> None:
        self.setWindowTitle(f"Edit Script — {path.name}")
        if path.suffix.lower() == ".py":
            self.developer.load(path)
            self.tabs.setCurrentWidget(self.developer)
            return
        try:
            self.builder.load(json.loads(path.read_text(encoding="utf-8")))
            self.builder.saved_path = path
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not open script", str(exc))
        self.tabs.setCurrentWidget(self.builder)
