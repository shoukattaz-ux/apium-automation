"""Schedules dialog: run scripts automatically at set times or intervals."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout,
    QWidget,
)

from core.library import ScriptEntry, list_scripts, resolve_script
from core.scheduler import WEEKDAY_NAMES, Schedule, ScheduleStore

from .run_dialog import TargetPicker, make_repeat_controls

if TYPE_CHECKING:
    from .dashboard import MainWindow


class ScheduleEditDialog(QDialog):
    def __init__(self, parent, schedule: Schedule | None, devices: list[tuple[str, str]]):
        super().__init__(parent)
        self.setWindowTitle("Edit Schedule" if schedule else "New Schedule")
        self.setMinimumWidth(540)
        self.schedule = schedule
        self.scripts = list_scripts()
        self.result_schedule: Schedule | None = None

        self.name = QLineEdit(schedule.name if schedule else "")
        self.name.setPlaceholderText("e.g. Morning order sync")
        self.script_combo = QComboBox()
        for entry in self.scripts:
            self.script_combo.addItem(entry.display_name, entry.relative_name())
        if schedule:
            index = self.script_combo.findData(schedule.script)
            if index < 0:
                self.script_combo.addItem(f"{schedule.script} (missing)", schedule.script)
                index = self.script_combo.count() - 1
            self.script_combo.setCurrentIndex(index)

        self.kind = QComboBox()
        self.kind.addItem("Every day / chosen days at a time", "daily")
        self.kind.addItem("Every N minutes", "interval")
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm")
        self.days: list[QCheckBox] = []
        days_row = QHBoxLayout()
        for index, name in enumerate(WEEKDAY_NAMES):
            box = QCheckBox(name)
            box.setChecked(True)
            self.days.append(box)
            days_row.addWidget(box)
        daily = QWidget()
        daily_form = QFormLayout(daily)
        daily_form.setContentsMargins(0, 0, 0, 0)
        daily_form.addRow("At", self.time)
        daily_form.addRow("On", days_row)
        self.every = QSpinBox()
        self.every.setRange(1, 7 * 24 * 60)
        self.every.setSuffix(" min")
        self.every.setValue(60)
        interval = QWidget()
        interval_form = QFormLayout(interval)
        interval_form.setContentsMargins(0, 0, 0, 0)
        interval_form.addRow("Every", self.every)
        self.when = QStackedWidget()
        self.when.addWidget(daily)
        self.when.addWidget(interval)
        self.kind.currentIndexChanged.connect(self.when.setCurrentIndex)

        self.picker = TargetPicker(devices)
        if schedule:
            for serial in list(schedule.serials) + list(schedule.roles.values()):
                self.picker.ensure_device(serial)
        self.repeat, self.delay = make_repeat_controls()
        self.repeat.setMinimum(1)
        self.repeat.setSpecialValueText("")
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(True)
        self.error = QLabel()
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        self.error.hide()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.addRow("Name", self.name)
        form.addRow("Script", self.script_combo)
        form.addRow("When", self.kind)
        form.addRow("", self.when)
        form.addRow(self.picker)
        form.addRow("Run", self.repeat)
        form.addRow("Pause between runs", self.delay)
        form.addRow("", self.enabled)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

        self.script_combo.currentIndexChanged.connect(lambda _: self._script_changed())
        self._script_changed()
        if schedule:
            self._load(schedule)

    def _entry(self) -> ScriptEntry | None:
        relative = self.script_combo.currentData()
        return next((e for e in self.scripts if e.relative_name() == relative), None)

    def _script_changed(self) -> None:
        preselect = []
        if self.schedule:
            preselect = list(self.schedule.roles.values()) or list(self.schedule.serials)
        self.picker.set_script(self._entry(), preselect)
        if self.schedule and self.schedule.roles:
            self.picker.set_mapping(self.schedule.roles)

    def _load(self, schedule: Schedule) -> None:
        self.kind.setCurrentIndex(0 if schedule.kind == "daily" else 1)
        hour, minute = (int(p) for p in schedule.time.split(":"))
        self.time.setTime(QTime(hour, minute))
        for index, box in enumerate(self.days):
            box.setChecked(index in schedule.weekdays)
        self.every.setValue(int(schedule.every_minutes))
        self.repeat.setValue(max(1, int(schedule.repeat)))
        self.delay.setValue(float(schedule.delay_seconds))
        self.enabled.setChecked(schedule.enabled)

    def _accept(self) -> None:
        entry = self._entry()
        workflow = bool(entry and entry.is_workflow)
        schedule = Schedule(
            name=self.name.text().strip(),
            script=self.script_combo.currentData() or "",
            kind=self.kind.currentData(),
            time=self.time.time().toString("HH:mm"),
            weekdays=[i for i, box in enumerate(self.days) if box.isChecked()],
            every_minutes=self.every.value(),
            serials=[] if workflow else self.picker.serials(),
            roles=self.picker.mapping() if workflow else {},
            repeat=self.repeat.value(),
            delay_seconds=self.delay.value(),
            enabled=self.enabled.isChecked(),
        )
        if self.schedule:
            schedule.id = self.schedule.id
            schedule.last_run = self.schedule.last_run
        problems = schedule.validate() + [p for p in self.picker.problems(allow_busy=True)
                                          if p not in schedule.validate()]
        if entry is None and schedule.script:
            problems.append("That script no longer exists")
        self.error.setText("\n".join(dict.fromkeys(problems)))
        self.error.setVisible(bool(problems))
        if problems:
            return
        self.result_schedule = schedule
        self.accept()


class SchedulesDialog(QDialog):
    COLUMNS = ["On", "Name", "Script", "When / phones", "Next run", "Last run"]

    def __init__(self, dashboard: "MainWindow", store: ScheduleStore):
        super().__init__(dashboard)
        self.setWindowTitle("Schedules")
        self.resize(900, 460)
        self.dashboard = dashboard
        self.store = store

        intro = QLabel("Scheduled runs start automatically while this app is open. "
                       "A phone that is busy or unplugged at that moment is skipped (see its log).")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda _: self.edit())
        self.table.itemChanged.connect(self._toggled)

        add = QPushButton("＋  New Schedule")
        add.setObjectName("Primary")
        add.clicked.connect(self.add)
        edit = QPushButton("Edit…")
        edit.clicked.connect(self.edit)
        run_now = QPushButton("▶  Run Now")
        run_now.clicked.connect(self.run_now)
        delete = QPushButton("Delete")
        delete.setObjectName("Danger")
        delete.clicked.connect(self.delete)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(edit)
        buttons.addWidget(run_now)
        buttons.addWidget(delete)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        now = datetime.now()
        for schedule in self.store.schedules:
            row = self.table.rowCount()
            self.table.insertRow(row)
            enabled = QTableWidgetItem()
            enabled.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            enabled.setCheckState(Qt.Checked if schedule.enabled else Qt.Unchecked)
            enabled.setData(Qt.UserRole, schedule.id)
            upcoming = schedule.next_run(now)
            last = schedule.last_run.replace("T", " ") if schedule.last_run else "never"
            values = [schedule.name, schedule.script, schedule.describe(),
                      upcoming.strftime("%a %H:%M") if upcoming else "—", last]
            self.table.setItem(row, 0, enabled)
            for column, value in enumerate(values, start=1):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.blockSignals(False)

    def _selected(self) -> Schedule | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        schedule_id = self.table.item(row, 0).data(Qt.UserRole)
        return next((s for s in self.store.schedules if s.id == schedule_id), None)

    def _toggled(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        schedule = next((s for s in self.store.schedules if s.id == item.data(Qt.UserRole)), None)
        if schedule:
            schedule.enabled = item.checkState() == Qt.Checked
            self.store.upsert(schedule)
            self.reload()

    def add(self) -> None:
        dialog = ScheduleEditDialog(self, None, self.dashboard.connected_devices())
        if dialog.exec() == QDialog.Accepted and dialog.result_schedule:
            self.store.upsert(dialog.result_schedule)
            self.reload()

    def edit(self) -> None:
        schedule = self._selected()
        if not schedule:
            return
        dialog = ScheduleEditDialog(self, schedule, self.dashboard.connected_devices())
        if dialog.exec() == QDialog.Accepted and dialog.result_schedule:
            self.store.upsert(dialog.result_schedule)
            self.reload()

    def run_now(self) -> None:
        schedule = self._selected()
        if schedule:
            self.dashboard.run_schedule(schedule)
            self.reload()

    def delete(self) -> None:
        schedule = self._selected()
        if schedule and QMessageBox.question(self, "Delete schedule?",
                                             f"Delete “{schedule.name}”?") == QMessageBox.Yes:
            self.store.remove(schedule.id)
            self.reload()


def schedule_script_path(schedule: Schedule):
    return resolve_script(schedule.script)
