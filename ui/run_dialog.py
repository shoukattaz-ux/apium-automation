"""Choosing what to run where: the shared phone picker and the Run dialog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QLabel, QSpinBox,
    QVBoxLayout, QWidget,
)

from core.library import ScriptEntry


class TargetPicker(QWidget):
    """Picks phones for a script: one phone per role for workflows, a set of phones otherwise."""

    changed = Signal()

    def __init__(self, devices: list[tuple[str, str]], busy: set[str] | None = None, parent=None):
        super().__init__(parent)
        self.devices = list(devices)
        self.busy = busy or set()
        self.entry: ScriptEntry | None = None
        self.role_combos: dict[str, QComboBox] = {}
        self.checks: dict[str, QCheckBox] = {}
        self.layout_ = QFormLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def _device_label(self, serial: str, name: str) -> str:
        suffix = "  (busy)" if serial in self.busy else ""
        return f"{name} ({serial}){suffix}" if name != serial else f"{serial}{suffix}"

    def ensure_device(self, serial: str) -> None:
        """Make a (possibly disconnected) phone selectable, e.g. one stored in a schedule."""
        if serial and serial not in [s for s, _ in self.devices]:
            self.devices.append((serial, f"{serial} — not connected"))

    def set_script(self, entry: ScriptEntry | None, preselect: list[str] | None = None) -> None:
        self.entry = entry
        while self.layout_.rowCount():
            self.layout_.removeRow(0)
        self.role_combos.clear()
        self.checks.clear()
        preselect = [s for s in (preselect or []) if s]
        if not self.devices:
            note = QLabel("No phones connected.")
            note.setObjectName("Muted")
            self.layout_.addRow(note)
            return
        if entry is not None and entry.is_workflow:
            order = preselect + [s for s, _ in self.devices if s not in preselect and s not in self.busy] + \
                [s for s, _ in self.devices if s not in preselect and s in self.busy]
            for index, role in enumerate(entry.roles):
                combo = QComboBox()
                for serial, name in self.devices:
                    combo.addItem(self._device_label(serial, name), serial)
                if index < len(order):
                    combo.setCurrentIndex(combo.findData(order[index]))
                combo.currentIndexChanged.connect(lambda _=0: self.changed.emit())
                self.role_combos[role] = combo
                self.layout_.addRow(f"Phone {role}", combo)
        else:
            holder = QWidget()
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 0, 0, 0)
            for serial, name in self.devices:
                check = QCheckBox(self._device_label(serial, name))
                check.setChecked(serial in preselect if preselect else serial not in self.busy)
                check.toggled.connect(lambda _=False: self.changed.emit())
                self.checks[serial] = check
                column.addWidget(check)
            self.layout_.addRow("Phones", holder)
        self.changed.emit()

    def mapping(self) -> dict[str, str]:
        return {role: combo.currentData() for role, combo in self.role_combos.items()}

    def serials(self) -> list[str]:
        if self.role_combos:
            return list(self.mapping().values())
        return [serial for serial, check in self.checks.items() if check.isChecked()]

    def set_mapping(self, mapping: dict[str, str]) -> None:
        for role, serial in mapping.items():
            combo = self.role_combos.get(role)
            if combo is not None:
                index = combo.findData(serial)
                if index >= 0:
                    combo.setCurrentIndex(index)

    def problems(self, allow_busy: bool = False) -> list[str]:
        chosen = self.serials()
        problems = []
        if not chosen:
            problems.append("Choose at least one phone")
        if self.role_combos and len(set(chosen)) != len(chosen):
            problems.append("Each role needs a different phone")
        busy = [s for s in chosen if s in self.busy]
        if busy and not allow_busy:
            problems.append(f"Busy: {', '.join(busy)} — stop it first or pick another phone")
        return problems


@dataclass
class RunRequest:
    path: Path
    entry: ScriptEntry
    mapping: dict[str, str] | None   # workflows
    serials: list[str]               # single-phone scripts (one run per phone)
    repeat: int
    delay_seconds: float


def make_repeat_controls() -> tuple[QSpinBox, QDoubleSpinBox]:
    repeat = QSpinBox()
    repeat.setRange(0, 100000)
    repeat.setValue(1)
    repeat.setSpecialValueText("Until stopped")
    repeat.setSuffix(" time(s)")
    delay = QDoubleSpinBox()
    delay.setRange(0, 24 * 3600)
    delay.setDecimals(0)
    delay.setSuffix(" s")
    delay.setToolTip("Pause between repeated runs")
    return repeat, delay


class RunDialog(QDialog):
    """Run any script: pick the phones (or a phone per role), how many times, and the pause between runs."""

    def __init__(self, parent, scripts: list[ScriptEntry], devices: list[tuple[str, str]], busy: set[str],
                 script_path: Path | None = None, serial: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Run Script")
        self.setMinimumWidth(520)
        self.scripts = scripts
        self.result_request: RunRequest | None = None
        self._preselect = [serial] if serial else []

        self.script_combo = QComboBox()
        for entry in scripts:
            self.script_combo.addItem(entry.display_name, str(entry.path))
        if script_path is not None:
            index = self.script_combo.findData(str(script_path))
            if index >= 0:
                self.script_combo.setCurrentIndex(index)
        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        self.picker = TargetPicker(devices, busy)
        self.repeat, self.delay = make_repeat_controls()
        self.error = QLabel()
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        self.error.hide()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        start = buttons.button(QDialogButtonBox.Ok)
        start.setText("▶  Start")
        start.setObjectName("Primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.addRow("Script", self.script_combo)
        form.addRow("", self.hint)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        form.addRow(line)
        form.addRow(self.picker)
        form.addRow("Run", self.repeat)
        form.addRow("Pause between runs", self.delay)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addStretch(1)
        layout.addWidget(buttons)

        self.script_combo.currentIndexChanged.connect(lambda _: self._script_changed())
        self._script_changed()

    def current_entry(self) -> ScriptEntry | None:
        path = self.script_combo.currentData()
        return next((e for e in self.scripts if str(e.path) == path), None)

    def _script_changed(self) -> None:
        entry = self.current_entry()
        if entry is None:
            self.hint.setText("No scripts yet — create one with New Script.")
        elif entry.is_workflow:
            self.hint.setText(f"Cross-phone workflow: choose a phone for each role "
                              f"({', '.join(entry.roles)}). Steps run in order across the phones.")
        else:
            self.hint.setText("Runs independently on every phone you tick.")
        self.picker.set_script(entry, self._preselect)

    def _accept(self) -> None:
        entry = self.current_entry()
        problems = ["Choose a script"] if entry is None else self.picker.problems()
        self.error.setText("\n".join(problems))
        self.error.setVisible(bool(problems))
        if problems:
            return
        workflow = entry.is_workflow
        self.result_request = RunRequest(
            path=entry.path, entry=entry, mapping=self.picker.mapping() if workflow else None,
            serials=self.picker.serials(), repeat=self.repeat.value(), delay_seconds=self.delay.value())
        self.accept()
