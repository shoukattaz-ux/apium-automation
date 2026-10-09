"""Phases 4 and 5: the New Script window.

Two tabs share one window:

* **Script Builder** — non-technical users add steps through forms; the result
  is saved as ``configs/<name>.json`` without anyone seeing JSON.
* **Developer Mode** — technical users write ``def run(device): ...`` in Python
  using the same DeviceSession action API, run it on a phone, and save it to
  ``configs/scripts/<name>.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFontDatabase, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from core import paths
from core.devices import DeviceError, list_installed_packages
from core.schema import (
    ACTION_LABELS, ACTIONS, FieldSpec, ScriptError, describe_step, safe_filename, save_script,
    validate_script, validate_step,
)

from .highlighter import PythonHighlighter

if TYPE_CHECKING:
    from .dashboard import MainWindow

DEVELOPER_TEMPLATE = '''\
def run(device):
    """Called with the DeviceSession of the phone you pick.

    Actions: open_app, click, wait_for_element, copy_text, paste_text,
    scroll, wait, tap, back, current_package.
    Locator types: "id", "xpath", "accessibility id", "text", "class name".
    """
    device.open_app("com.example.app")
    device.wait_for_element("id", "com.example.app:id/title", timeout_seconds=15)
    value = device.copy_text("id", "com.example.app:id/title")
    print("Copied:", value)
    device.scroll("down", times=2)
'''


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


class StepDialog(QDialog):
    """Add/edit one step. Fields are generated from ``core.schema.ACTIONS``."""

    def __init__(self, parent=None, step: dict | None = None, variables: list[str] | None = None,
                 devices: list[tuple[str, str]] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Step" if step else "Add Step")
        self.setMinimumWidth(600)
        self.variables = variables or []
        self.devices = devices or []
        self.inputs: dict[str, QWidget] = {}
        self.errors: dict[str, QLabel] = {}
        self.result_step: dict | None = None

        self.action_combo = QComboBox()
        for key, label in ACTION_LABELS.items():
            self.action_combo.addItem(label, key)
        self.action_error = QLabel()
        self.action_error.setObjectName("Error")
        self.action_error.hide()

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 8, 0, 0)
        self.form.setVerticalSpacing(4)
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Save Step")
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        top = QFormLayout()
        top.addRow("Action", self.action_combo)
        top.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.action_error)
        layout.addWidget(self.form_host)
        layout.addStretch(1)
        layout.addWidget(buttons)

        if step:
            index = self.action_combo.findData(step.get("action"))
            self.action_combo.setCurrentIndex(max(0, index))
        self.action_combo.currentIndexChanged.connect(lambda _: self._build_fields())
        self._build_fields(step)

    def action(self) -> str:
        return self.action_combo.currentData()

    def _build_fields(self, step: dict | None = None) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs.clear()
        self.errors.clear()
        self.action_error.hide()
        for spec in ACTIONS[self.action()].fields:
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
            if spec.name == "package":
                row = QHBoxLayout()
                row.addWidget(widget, 1)
                detect = QPushButton("Detect from connected device")
                detect.setEnabled(bool(self.devices))
                detect.setToolTip("" if self.devices else "Connect a phone first")
                detect.clicked.connect(self._detect_package)
                row.addWidget(detect)
                column.addLayout(row)
            else:
                column.addWidget(widget)
            if spec.help:
                hint = QLabel(spec.help)
                hint.setObjectName("Muted")
                hint.setWordWrap(True)
                column.addWidget(hint)
            column.addWidget(error)
            self.form.addRow(spec.label + ("" if spec.required else " (optional)"), container)
        self.adjustSize()

    def _make_input(self, spec: FieldSpec, value: Any) -> QWidget:
        if spec.kind in ("choice", "locator_type"):
            combo = QComboBox()
            combo.addItems(list(spec.choices))
            if value in spec.choices:
                combo.setCurrentText(value)
            return combo
        if spec.kind == "int":
            box = QSpinBox()
            box.setRange(int(spec.minimum or 0), 1000)
            box.setValue(int(value) if value not in (None, "") else int(spec.minimum or 0))
            return box
        if spec.kind == "float":
            box = QDoubleSpinBox()
            box.setRange(float(spec.minimum or 0), 3600)
            box.setDecimals(1)
            box.setSingleStep(0.5)
            box.setSuffix(" s")
            box.setValue(float(value) if value not in (None, "") else 0.0)
            return box
        if spec.name == "value_from":
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")
            combo.addItems(self.variables)
            combo.setCurrentText("" if value is None else str(value))
            return combo
        line = QLineEdit("" if value is None else str(value))
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

    def current_step(self) -> dict:
        step = {"action": self.action()}
        for name, widget in self.inputs.items():
            value = self._value(widget)
            if value != "":
                step[name] = value
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
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.action_error.setText(problems.get("action", ""))
        self.action_error.setVisible("action" in problems)
        if problems:
            return
        self.result_step = step
        self.accept()


class StepCard(QFrame):
    edit = Signal(int)
    delete = Signal(int)
    move = Signal(int, int)

    def __init__(self, index: int, total: int, step: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("StepCard")
        number = QLabel(f"{index + 1}")
        number.setObjectName("Muted")
        number.setFixedWidth(22)
        number.setAlignment(Qt.AlignCenter)
        title = QLabel(ACTION_LABELS.get(step.get("action"), step.get("action", "?")))
        title.setObjectName("DeviceName")
        summary = QLabel(describe_step(step))
        summary.setObjectName("Muted")
        summary.setWordWrap(True)

        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(title)
        text.addWidget(summary)

        def button(label: str, tip: str, slot, enabled: bool = True) -> QPushButton:
            b = QPushButton(label)
            b.setObjectName("Icon")
            b.setToolTip(tip)
            b.setEnabled(enabled)
            b.clicked.connect(slot)
            return b

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(number)
        layout.addLayout(text, 1)
        layout.addWidget(button("▲", "Move up", lambda: self.move.emit(index, index - 1), index > 0))
        layout.addWidget(button("▼", "Move down", lambda: self.move.emit(index, index + 1), index < total - 1))
        layout.addWidget(button("✎", "Edit step", lambda: self.edit.emit(index)))
        delete = button("✕", "Delete step", lambda: self.delete.emit(index))
        delete.setObjectName("Danger")
        layout.addWidget(delete)


# ======================================================================= tabs


class BuilderTab(QWidget):
    saved = Signal(Path)

    def __init__(self, dashboard: "MainWindow | None", parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.steps: list[dict] = []

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Copy order number to sheet")
        self.name_error = QLabel()
        self.name_error.setObjectName("Error")
        self.name_error.hide()

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
        add.clicked.connect(self.add_step)
        save = QPushButton("Save Script")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        self.saved_label = QLabel()
        self.saved_label.setObjectName("Muted")

        name_row = QFormLayout()
        name_row.addRow("Script name", self.name_edit)
        steps_header = QHBoxLayout()
        steps_title = QLabel("Steps")
        steps_title.setObjectName("PanelTitle")
        steps_header.addWidget(steps_title)
        steps_header.addStretch(1)
        steps_header.addWidget(add)
        footer = QHBoxLayout()
        footer.addWidget(self.saved_label, 1)
        footer.addWidget(save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addLayout(name_row)
        layout.addWidget(self.name_error)
        layout.addLayout(steps_header)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.steps_error)
        layout.addLayout(footer)
        self._render()

    def load(self, script: dict) -> None:
        self.name_edit.setText(script.get("name", ""))
        self.steps = [dict(step) for step in script.get("steps", [])]
        self._render()

    def _devices(self) -> list[tuple[str, str]]:
        return self.dashboard.connected_devices() if self.dashboard else []

    def _variables_before(self, index: int) -> list[str]:
        return [s["save_as"] for s in self.steps[:index] if s.get("action") == "copy_text" and s.get("save_as")]

    def _render(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if not self.steps:
            empty = QLabel("No steps yet. Click “Add Step” to start.")
            empty.setObjectName("EmptyState")
            empty.setAlignment(Qt.AlignCenter)
            self.cards_layout.addWidget(empty)
        for index, step in enumerate(self.steps):
            card = StepCard(index, len(self.steps), step)
            card.edit.connect(self.edit_step)
            card.delete.connect(self.delete_step)
            card.move.connect(self.move_step)
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)

    def add_step(self) -> None:
        dialog = StepDialog(self, variables=self._variables_before(len(self.steps)), devices=self._devices())
        if dialog.exec() == QDialog.Accepted and dialog.result_step:
            self.steps.append(dialog.result_step)
            self._render()

    def edit_step(self, index: int) -> None:
        dialog = StepDialog(self, step=self.steps[index], variables=self._variables_before(index),
                            devices=self._devices())
        if dialog.exec() == QDialog.Accepted and dialog.result_step:
            self.steps[index] = dialog.result_step
            self._render()

    def delete_step(self, index: int) -> None:
        del self.steps[index]
        self._render()

    def move_step(self, source: int, target: int) -> None:
        if 0 <= target < len(self.steps):
            self.steps.insert(target, self.steps.pop(source))
            self._render()

    def save(self) -> None:
        name = self.name_edit.text().strip()
        script = {"name": name, "steps": self.steps}
        self.name_error.setVisible(not name)
        self.name_error.setText("Give the script a name" if not name else "")
        problems = [p for p in validate_script(script) if p != "Script needs a name"]
        self.steps_error.setText("\n".join(problems))
        self.steps_error.setVisible(bool(problems))
        if not name or problems:
            return
        target = paths.configs_dir() / f"{safe_filename(name)}.json"
        if target.exists():
            answer = QMessageBox.question(self, "Replace script?",
                                          f"A script called “{target.stem}” already exists. Replace it?")
            if answer != QMessageBox.Yes:
                return
        try:
            path = save_script(script, paths.configs_dir())
        except (ScriptError, OSError) as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.saved_label.setText(f"Saved to {path.name}")
        self.saved.emit(path)


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
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)
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
        self.resize(820, 680)

        self.builder = BuilderTab(dashboard)
        self.developer = DeveloperTab(dashboard)
        self.builder.saved.connect(self.saved)
        self.developer.saved.connect(self.saved)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.builder, "Script Builder")
        self.tabs.addTab(self.developer, "Developer Mode")
        self.tabs.currentChanged.connect(lambda i: self.developer.refresh_devices() if i == 1 else None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.tabs)

        if script_path:
            self.open_path(Path(script_path))

    def open_path(self, path: Path) -> None:
        self.setWindowTitle(f"Edit Script — {path.name}")
        if path.suffix.lower() == ".py":
            self.developer.load(path)
            self.tabs.setCurrentWidget(self.developer)
            return
        try:
            self.builder.load(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not open script", str(exc))
        self.tabs.setCurrentWidget(self.builder)
