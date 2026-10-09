"""The Run History tab: every run with its result, duration and a link to its report."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import paths
from core.history import REPORT_HTML, RunRecord, export_csv, format_duration, list_runs

from .theme import STATUS_COLORS, STATUS_LABELS

COLUMNS = ["Started", "Script", "Phones", "Result", "Duration", "Steps", "Trigger"]


class HistoryPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs: list[tuple[Path, RunRecord]] = []

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by script or phone…")
        self.search.textChanged.connect(self._apply_filter)
        self.result_filter = QComboBox()
        self.result_filter.addItem("All results", "")
        for state in ("completed", "partial", "failed", "stopped"):
            self.result_filter.addItem(STATUS_LABELS.get(state, state), state)
        self.result_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        refresh = QPushButton("⟳")
        refresh.setObjectName("Icon")
        refresh.setToolTip("Reload history")
        refresh.clicked.connect(self.reload)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.resizeSection(2, 170)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.doubleClicked.connect(lambda _: self.open_report())
        self.table.itemSelectionChanged.connect(self._selection_changed)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        self.open_button = QPushButton("Open Report")
        self.open_button.setObjectName("Primary")
        self.open_button.clicked.connect(self.open_report)
        self.folder_button = QPushButton("Open Folder")
        self.folder_button.clicked.connect(self.open_folder)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export)
        clear = QPushButton("Clear History…")
        clear.setObjectName("Danger")
        clear.clicked.connect(self.clear)

        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        filters.addWidget(self.result_filter)
        filters.addWidget(refresh)
        footer = QHBoxLayout()
        footer.addWidget(self.summary, 1)
        footer.addWidget(clear)
        footer.addWidget(export)
        footer.addWidget(self.folder_button)
        footer.addWidget(self.open_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(filters)
        layout.addWidget(self.table, 1)
        layout.addLayout(footer)
        self.reload()

    def reload(self) -> None:
        self.runs = list_runs()
        self._apply_filter()

    def _visible_runs(self) -> list[tuple[Path, RunRecord]]:
        needle = self.search.text().strip().lower()
        state = self.result_filter.currentData()
        result = []
        for folder, run in self.runs:
            if state and run.state != state:
                continue
            haystack = f"{run.script} {' '.join(run.devices.values())} {' '.join(run.devices)} {run.trigger}".lower()
            if needle and needle not in haystack:
                continue
            result.append((folder, run))
        return result

    def _apply_filter(self) -> None:
        visible = self._visible_runs()
        self.table.setRowCount(0)
        for folder, run in visible:
            row = self.table.rowCount()
            self.table.insertRow(row)
            counts = run.counts()
            phones = ", ".join(serial if role == "device" else f"{role}={serial}" for role, serial in run.devices.items())
            steps = f"{counts['ok']} ok" + (f" · {counts['skipped']} skipped" if counts["skipped"] else "") + \
                (f" · {counts['failed']} failed" if counts["failed"] else "")
            values = [
                datetime.fromtimestamp(run.started_at).strftime("%Y-%m-%d %H:%M:%S"),
                run.script + (f"  (#{run.run_number})" if run.run_number > 1 else ""),
                phones,
                STATUS_LABELS.get(run.state, run.state),
                format_duration(run.duration),
                steps,
                run.trigger,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, str(folder))
                if column == 3:
                    item.setForeground(QColor(STATUS_COLORS.get(run.state, "#9aa1ad")))
                if run.message:
                    item.setToolTip(run.message)
                self.table.setItem(row, column, item)
        failed = sum(1 for _, r in visible if r.state == "failed")
        self.summary.setText(f"{len(visible)} run(s)" + (f" · {failed} failed" if failed else ""))
        self._selection_changed()

    def _selection_changed(self) -> None:
        has = self.selected_folder() is not None
        self.open_button.setEnabled(has)
        self.folder_button.setEnabled(has)

    def selected_folder(self) -> Path | None:
        row = self.table.currentRow()
        if row < 0 or not self.table.selectedItems():
            return None
        item = self.table.item(row, 0)
        return Path(item.data(Qt.UserRole)) if item else None

    def open_report(self) -> None:
        folder = self.selected_folder()
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder / REPORT_HTML)))

    def open_folder(self) -> None:
        folder = self.selected_folder() or paths.runs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def export(self) -> None:
        default = paths.app_dir() / f"run-history-{datetime.now():%Y%m%d}.csv"
        target, _ = QFileDialog.getSaveFileName(self, "Export run history", str(default), "CSV files (*.csv)")
        if target:
            export_csv(self._visible_runs(), Path(target))

    def clear(self) -> None:
        if not self.runs:
            return
        answer = QMessageBox.question(self, "Clear history?",
                                      f"Delete all {len(self.runs)} run reports and their screenshots?")
        if answer == QMessageBox.Yes:
            for folder, _ in self.runs:
                shutil.rmtree(folder, ignore_errors=True)
            self.reload()
