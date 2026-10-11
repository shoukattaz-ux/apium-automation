"""Script Library window (scripts and groups: new, edit, duplicate, rename, delete) and
the group editor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout,
)

from core import paths
from core.groups import MAX_RETRIES, Group, load_group, save_group, validate_group
from core.library import (
    ScriptEntry, delete_script, duplicate_script, list_scripts, references_to, rename_script,
)
from core.schema import ScriptError


class GroupEditorDialog(QDialog):
    """Pick scripts (in order) and how failures are handled."""

    def __init__(self, parent=None, group: Group | None = None):
        super().__init__(parent)
        self.group = group or Group(name="")
        self.setWindowTitle("Edit Group" if group else "New Group")
        self.setMinimumSize(720, 520)
        self.saved_path: Path | None = None
        self.available = [e for e in list_scripts(groups=False) if not e.is_workflow]
        names = {e.relative_name(): e.name for e in self.available}

        self.name = QLineEdit(self.group.name)
        self.name.setPlaceholderText("e.g. Morning routine")

        # Left: all scripts; right: the group's scripts in run order.
        self.library = QListWidget()
        self.library.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for entry in self.available:
            item = QListWidgetItem(entry.display_name)
            item.setData(Qt.UserRole, entry.relative_name())
            self.library.addItem(item)
        self.library.itemDoubleClicked.connect(lambda _: self._add())
        self.chosen = QListWidget()
        self.chosen.setDragDropMode(QAbstractItemView.InternalMove)
        for relative in self.group.scripts:
            self._append(relative, names.get(relative, f"{relative} (missing)"))
        self.chosen.itemDoubleClicked.connect(lambda _: self._remove())

        add = QPushButton("Add  →")
        add.setObjectName("Primary")
        add.clicked.connect(self._add)
        remove = QPushButton("←  Remove")
        remove.clicked.connect(self._remove)
        up = QPushButton("▲  Up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("▼  Down")
        down.clicked.connect(lambda: self._move(1))
        middle = QVBoxLayout()
        middle.addStretch(1)
        for button in (add, remove, up, down):
            middle.addWidget(button)
        middle.addStretch(1)

        left = QVBoxLayout()
        left.addWidget(QLabel("Saved scripts"))
        left.addWidget(self.library, 1)
        right = QVBoxLayout()
        right.addWidget(QLabel("Runs in this order (drag to reorder)"))
        right.addWidget(self.chosen, 1)
        lists = QHBoxLayout()
        lists.addLayout(left, 1)
        lists.addLayout(middle)
        lists.addLayout(right, 1)

        # Failure handling
        self.retries = QSpinBox()
        self.retries.setRange(0, MAX_RETRIES)
        self.retries.setValue(self.group.retries)
        self.retries.setSuffix(" extra attempt(s)")
        self.retries.setToolTip("How many more times a failing script is tried before it counts as failed")
        self.on_fail = QComboBox()
        self.on_fail.addItem("Skip it and go on with the next script", "skip")
        self.on_fail.addItem("Stop the whole group", "stop")
        self.on_fail.setCurrentIndex(max(0, self.on_fail.findData(self.group.on_script_fail)))
        self.alert = QCheckBox("Ring the alarm when a script still fails")
        self.alert.setChecked(self.group.alert)
        self.retry_partial = QCheckBox("Treat a script that skipped steps as failed (retry it)")
        self.retry_partial.setChecked(self.group.retry_partial)
        self.max_minutes = QDoubleSpinBox()
        self.max_minutes.setRange(0, 24 * 60)
        self.max_minutes.setDecimals(1)
        self.max_minutes.setSuffix(" min")
        self.max_minutes.setSpecialValueText("No limit")
        self.max_minutes.setValue(self.group.max_minutes)
        self.max_minutes.setToolTip("A script still running after this long is stuck: it is stopped, "
                                    "logged with a screenshot, then retried or skipped")
        self.pause = QDoubleSpinBox()
        self.pause.setRange(0, 3600)
        self.pause.setDecimals(1)
        self.pause.setSuffix(" s")
        self.pause.setValue(self.group.pause_seconds)

        options = QGroupBox("When a script fails")
        form = QFormLayout(options)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.addRow("Retry", self.retries)
        form.addRow("Then", self.on_fail)
        form.addRow("Stuck after", self.max_minutes)
        form.addRow("", self.alert)
        form.addRow("", self.retry_partial)
        form.addRow("Pause between scripts", self.pause)

        self.error = QLabel()
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        self.error.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        name_row = QFormLayout()
        name_row.addRow("Group name", self.name)
        layout = QVBoxLayout(self)
        layout.addLayout(name_row)
        layout.addLayout(lists, 1)
        layout.addWidget(options)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def _append(self, relative: str, name: str) -> None:
        item = QListWidgetItem(name)
        item.setData(Qt.UserRole, relative)
        self.chosen.addItem(item)

    def _add(self) -> None:
        for item in self.library.selectedItems():
            self._append(item.data(Qt.UserRole), item.text())

    def _remove(self) -> None:
        for item in self.chosen.selectedItems():
            self.chosen.takeItem(self.chosen.row(item))

    def _move(self, offset: int) -> None:
        row = self.chosen.currentRow()
        target = row + offset
        if row < 0 or not 0 <= target < self.chosen.count():
            return
        item = self.chosen.takeItem(row)
        self.chosen.insertItem(target, item)
        self.chosen.setCurrentRow(target)

    def result_group(self) -> Group:
        return Group(
            name=self.name.text().strip(),
            scripts=[self.chosen.item(i).data(Qt.UserRole) for i in range(self.chosen.count())],
            retries=self.retries.value(), pause_seconds=self.pause.value(), max_minutes=self.max_minutes.value(),
            on_script_fail=self.on_fail.currentData(), alert=self.alert.isChecked(),
            retry_partial=self.retry_partial.isChecked(), path=self.group.path)

    def _save(self) -> None:
        group = self.result_group()
        problems = validate_group(group)
        self.error.setText("\n".join(problems))
        self.error.setVisible(bool(problems))
        if problems:
            return
        try:
            self.saved_path = save_group(group)
        except (ScriptError, OSError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class LibraryDialog(QDialog):
    """Every saved script and group, with the housekeeping buttons."""

    def __init__(self, parent=None, open_editor: Callable[[Path | None], object] | None = None,
                 run: Callable[[Path], None] | None = None, on_change: Callable[[], None] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Script Library")
        self.setMinimumSize(640, 460)
        self.open_editor = open_editor
        self.run = run
        self.on_change = on_change
        self.entries: list[ScriptEntry] = []

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search scripts and groups…")
        self.search.textChanged.connect(lambda _: self.reload())
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        self.list.currentRowChanged.connect(lambda _: self._selection_changed())
        self.info = QLabel()
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)

        def button(text: str, slot, tip: str, name: str = "") -> QPushButton:
            b = QPushButton(text)
            b.setToolTip(tip)
            if name:
                b.setObjectName(name)
            b.clicked.connect(lambda _=False: slot())
            return b

        self.new_button = button("＋  New Script", self.new_script, "Create a script")
        self.group_button = button("＋  New Group", self.new_group,
                                   "Chain several scripts to run one after another on a phone")
        self.edit_button = button("✎  Edit", self.edit, "Open the selected script or group")
        self.duplicate_button = button("⧉  Duplicate", self.duplicate, "Make a copy to change")
        self.rename_button = button("Rename…", self.rename, "Change the name shown in the app")
        self.delete_button = button("🗑  Delete…", self.delete, "Move to the deleted folder", "Danger")
        self.run_button = button("▶  Run…", self.run_selected, "Run the selected script or group", "Primary")
        close = button("Close", self.accept, "")

        side = QVBoxLayout()
        for b in (self.new_button, self.group_button, None, self.edit_button, self.duplicate_button,
                  self.rename_button, self.delete_button, None, self.run_button):
            if b is None:
                side.addSpacing(10)
            else:
                side.addWidget(b)
        side.addStretch(1)
        side.addWidget(close)

        left = QVBoxLayout()
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        left.addWidget(self.info)
        layout = QHBoxLayout(self)
        layout.addLayout(left, 1)
        layout.addLayout(side)
        self.reload()

    # ------------------------------------------------------------------ list

    def reload(self, select: Path | None = None) -> None:
        current = select or (self.selected().path if self.selected() else None)
        needle = self.search.text().strip().lower()
        self.entries = [e for e in list_scripts() if not needle or needle in e.display_name.lower()]
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            item = QListWidgetItem(entry.display_name)
            item.setData(Qt.UserRole, str(entry.path))
            item.setToolTip(entry.relative_name())
            self.list.addItem(item)
            if current and Path(current) == entry.path:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if self.list.currentRow() < 0 and self.entries:
            self.list.setCurrentRow(0)
        self._selection_changed()

    def selected(self) -> ScriptEntry | None:
        row = self.list.currentRow()
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def _selection_changed(self) -> None:
        entry = self.selected()
        for b in (self.edit_button, self.duplicate_button, self.delete_button, self.run_button):
            b.setEnabled(entry is not None)
        self.rename_button.setEnabled(entry is not None and entry.kind != "python")
        if entry is None:
            self.info.setText("No scripts yet." if not self.search.text() else "Nothing matches.")
            return
        uses = references_to(entry.path)
        text = f"File: {entry.relative_name()}"
        if uses:
            text += "\nUsed by: " + ", ".join(uses)
        self.info.setText(text)

    def _changed(self, select: Path | None = None) -> None:
        self.reload(select)
        if self.on_change:
            self.on_change()

    # ------------------------------------------------------------------ actions

    def new_script(self) -> None:
        if self.open_editor:
            self._watch(self.open_editor(None))

    def _watch(self, window) -> None:
        saved = getattr(window, "saved", None)
        if saved is not None:
            saved.connect(lambda *_: self._changed())

    def new_group(self) -> None:
        dialog = GroupEditorDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self._changed(dialog.saved_path)

    def edit(self) -> None:
        entry = self.selected()
        if entry is None:
            return
        if entry.is_group:
            try:
                group = load_group(entry.path)
            except ScriptError as exc:
                QMessageBox.warning(self, "Group problem", str(exc))
                return
            dialog = GroupEditorDialog(self, group)
            if dialog.exec() == QDialog.Accepted:
                self._changed(dialog.saved_path)
        elif self.open_editor:
            self._watch(self.open_editor(entry.path))

    def duplicate(self) -> None:
        entry = self.selected()
        if entry is None:
            return
        try:
            copy = duplicate_script(entry.path)
        except (ScriptError, OSError) as exc:
            QMessageBox.warning(self, "Couldn't duplicate", str(exc))
            return
        self._changed(copy)

    def rename(self) -> None:
        entry = self.selected()
        if entry is None or entry.kind == "python":
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if not ok or not name.strip() or name.strip() == entry.name:
            return
        try:
            rename_script(entry.path, name)
        except (ScriptError, OSError) as exc:
            QMessageBox.warning(self, "Couldn't rename", str(exc))
            return
        self._changed(entry.path)

    def delete(self) -> None:
        entry = self.selected()
        if entry is None:
            return
        uses = references_to(entry.path)
        warning = ("\n\nStill used by:\n  • " + "\n  • ".join(uses) +
                   "\nThose will report it as missing until you change them.") if uses else ""
        answer = QMessageBox.question(
            self, "Delete?",
            f"Delete “{entry.name}”?\n\nIt is moved to the “{paths.configs_dir().name}/deleted” folder, "
            f"so you can bring it back by moving the file out again.{warning}")
        if answer != QMessageBox.Yes:
            return
        try:
            delete_script(entry.path)
        except (ScriptError, OSError) as exc:
            QMessageBox.warning(self, "Couldn't delete", str(exc))
            return
        self._changed()

    def run_selected(self) -> None:
        entry = self.selected()
        if entry is not None and self.run:
            self.run(entry.path)
