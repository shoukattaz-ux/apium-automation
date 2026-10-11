"""The Errors tab: where and why automations failed or got stuck, with screenshots.

One row per problem (newest first) from ``core.errorlog``; selecting a row shows the
short details and the phone's screenshot at that moment. New entries arrive live
through ``add_entry`` (the dashboard forwards them from worker threads).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import errorlog, paths
from core.errorlog import ErrorEntry, ErrorLog
from core.history import REPORT_HTML

COLUMNS = ["Time", "Phone", "Script", "Problem", "Step", "Reason"]
KIND_COLORS = {
    errorlog.STUCK: "#f59e0b",
    errorlog.CONNECTION: "#f59e0b",
    errorlog.SCRIPT_FAILED: "#ef4444",
    errorlog.STEP_FAILED: "#ef4444",
    errorlog.STEP_SKIPPED: "#eab308",
    errorlog.NO_REACTION: "#eab308",
    errorlog.POPUP: "#38bdf8",
    errorlog.RECONNECTED: "#38bdf8",
    errorlog.APP_RETURNED: "#38bdf8",
}
KINDS = (errorlog.SCRIPT_FAILED, errorlog.STUCK, errorlog.STEP_FAILED, errorlog.STEP_SKIPPED, errorlog.CONNECTION,
         errorlog.NO_REACTION, errorlog.POPUP, errorlog.RECONNECTED, errorlog.APP_RETURNED)


class ErrorsPanel(QWidget):
    count_changed = Signal(int)  # entries not yet seen (for the tab title)

    def __init__(self, error_log: ErrorLog | None = None, parent=None):
        super().__init__(parent)
        self.error_log = error_log or ErrorLog()
        self.entries: list[ErrorEntry] = []
        self.unseen = 0

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by phone, script, group or reason…")
        self.search.textChanged.connect(lambda _: self._apply_filter())
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("All problems", "")
        for kind in KINDS:
            self.kind_filter.addItem(kind.capitalize(), kind)
        self.kind_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        refresh = QPushButton("⟳")
        refresh.setObjectName("Icon")
        refresh.setToolTip("Reload the errors log")
        refresh.clicked.connect(self.reload)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setTextElideMode(Qt.ElideRight)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.table.doubleClicked.connect(lambda _: self.open_screenshot())

        self.details = QLabel("Select a problem to see where it happened.")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.shot = QLabel("No screenshot")
        self.shot.setObjectName("Muted")
        self.shot.setAlignment(Qt.AlignCenter)
        self.shot.setMinimumSize(160, 220)
        self.shot.setCursor(Qt.PointingHandCursor)
        self.shot.setToolTip("Double-click the row (or Open Screenshot) to see it full size")
        detail_box = QWidget()
        detail_layout = QHBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.details, 1)
        detail_layout.addWidget(self.shot)
        self._pixmap: QPixmap | None = None

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(detail_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        self.shot_button = QPushButton("Open Screenshot")
        self.shot_button.clicked.connect(self.open_screenshot)
        self.report_button = QPushButton("Open Report")
        self.report_button.setObjectName("Primary")
        self.report_button.clicked.connect(self.open_report)
        folder = QPushButton("Open Folder")
        folder.clicked.connect(self.open_folder)
        clear = QPushButton("Clear…")
        clear.setObjectName("Danger")
        clear.clicked.connect(self.clear)

        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        filters.addWidget(self.kind_filter)
        filters.addWidget(refresh)
        footer = QHBoxLayout()
        footer.addWidget(self.summary, 1)
        footer.addWidget(clear)
        footer.addWidget(folder)
        footer.addWidget(self.shot_button)
        footer.addWidget(self.report_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(filters)
        layout.addWidget(split, 1)
        layout.addLayout(footer)
        self.reload()

    # ------------------------------------------------------------------ data

    def reload(self) -> None:
        self.entries = self.error_log.entries()
        self._apply_filter()

    def add_entry(self, entry: ErrorEntry) -> None:
        """A new problem from a running script (UI thread)."""
        self.entries.insert(0, entry)
        del self.entries[errorlog.MAX_ENTRIES_SHOWN:]
        self.unseen += 1
        self.count_changed.emit(self.unseen)
        self._apply_filter(keep_selection=True)

    def mark_seen(self) -> None:
        if self.unseen:
            self.unseen = 0
            self.count_changed.emit(0)

    def _visible(self) -> list[ErrorEntry]:
        needle = self.search.text().strip().lower()
        kind = self.kind_filter.currentData()
        return [e for e in self.entries
                if (not kind or e.kind == kind)
                and (not needle or needle in f"{e.device} {e.script} {e.group} {e.reason} {e.step}".lower())]

    def _apply_filter(self, keep_selection: bool = False) -> None:
        selected = self.selected_entry() if keep_selection else None
        visible = self._visible()
        self.table.setRowCount(0)
        self.table.setRowCount(len(visible))
        for row, entry in enumerate(visible):
            script = f"{entry.group} › {entry.script}" if entry.group else entry.script
            values = [entry.when, entry.device, script, entry.kind, entry.step, entry.reason]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, id(entry))
                if column == 3:
                    item.setForeground(QColor(KIND_COLORS.get(entry.kind, "#9aa1ad")))
                item.setToolTip(entry.reason)
                self.table.setItem(row, column, item)
            if entry is selected:
                self.table.selectRow(row)
        stuck = sum(1 for e in visible if e.kind == errorlog.STUCK)
        failed = sum(1 for e in visible if e.kind == errorlog.SCRIPT_FAILED)
        parts = [f"{len(visible)} problem(s)"]
        if failed:
            parts.append(f"{failed} script(s) failed")
        if stuck:
            parts.append(f"{stuck} stuck")
        self.summary.setText(" · ".join(parts))
        self._show_selected()

    def selected_entry(self) -> ErrorEntry | None:
        row = self.table.currentRow()
        if row < 0 or not self.table.selectedItems():
            return None
        item = self.table.item(row, 0)
        key = item.data(Qt.UserRole) if item else None
        return next((e for e in self.entries if id(e) == key), None)

    def _show_selected(self) -> None:
        entry = self.selected_entry()
        self.shot_button.setEnabled(bool(entry and entry.screenshot and Path(entry.screenshot).is_file()))
        self.report_button.setEnabled(bool(entry and entry.report and (Path(entry.report) / REPORT_HTML).is_file()))
        if entry is None:
            self.details.setText("Select a problem to see where it happened.")
            self._set_shot(None)
            return
        lines = [f"<b>{entry.kind.capitalize()}</b> — {entry.when}",
                 f"<b>Phone:</b> {entry.device}"]
        if entry.group:
            lines.append(f"<b>Group:</b> {entry.group}")
        lines.append(f"<b>Script:</b> {entry.script}")
        if entry.step:
            lines.append(f"<b>Step:</b> {entry.step}")
        if entry.attempts > 1:
            lines.append(f"<b>Attempts:</b> {entry.attempts}")
        lines.append(f"<b>Why:</b> {entry.reason}")
        self.details.setTextFormat(Qt.RichText)
        self.details.setText("<br>".join(_escape(line) for line in lines))
        self._set_shot(entry.screenshot)

    def _set_shot(self, path: str | None) -> None:
        pixmap = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        if pixmap.isNull():
            self._pixmap = None
            self.shot.setPixmap(QPixmap())
            self.shot.setText("No screenshot")
            return
        self._pixmap = pixmap
        self.shot.setPixmap(pixmap.scaled(self.shot.width(), self.shot.height(),
                                          Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._pixmap is not None:
            self.shot.setPixmap(self._pixmap.scaled(self.shot.width(), self.shot.height(),
                                                    Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ------------------------------------------------------------------ actions

    def open_screenshot(self) -> None:
        entry = self.selected_entry()
        if entry and entry.screenshot and Path(entry.screenshot).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(entry.screenshot))

    def open_report(self) -> None:
        entry = self.selected_entry()
        if entry and entry.report and (Path(entry.report) / REPORT_HTML).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(entry.report) / REPORT_HTML)))

    def open_folder(self) -> None:
        entry = self.selected_entry()
        folder = Path(entry.screenshot).parent if entry and entry.screenshot else paths.logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def clear(self) -> None:
        if not self.entries:
            return
        answer = QMessageBox.question(self, "Clear the errors log?",
                                      f"Remove all {len(self.entries)} entries from the errors log?\n"
                                      "(Run reports and their screenshots are kept.)")
        if answer == QMessageBox.Yes:
            self.error_log.clear()
            self.entries = []
            self.mark_seen()
            self._apply_filter()


def _escape(line: str) -> str:
    """Escape the values but keep the <b>labels</b> we put in."""
    import html

    if line.startswith("<b>"):
        label, _, rest = line.partition("</b>")
        return f"{label}</b>{html.escape(rest)}"
    return html.escape(line)
