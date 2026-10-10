"""The main dashboard: phones, per-phone script picker, Start/Stop, live logs and run history.

Also the hub for the other windows: script editor, Run dialog (repeats and
cross-phone workflows), element picker and schedules.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from core import paths
from core.appium_server import is_server_running
from core.devices import DeviceError, device_model, list_connected_devices
from core.library import ScriptEntry, list_scripts, resolve_script
from core.manager import DeviceManager
from core.runner import PythonScript, RunState, load_any_script, needs_role_mapping, roles_of, script_name
from core.scheduler import Schedule, ScheduleStore
from core.schema import ScriptError
from core.version import APP_NAME, APP_VERSION
from core.wireless import AutoReconnector, WirelessStore, is_wireless

from .history import HistoryPanel
from .qtutil import safe_emit
from .run_dialog import RunDialog, RunRequest
from .theme import STATUS_LABELS, status_dot_style

log = logging.getLogger(__name__)

SCAN_INTERVAL_MS = 4000
SCHEDULE_INTERVAL_MS = 15000
MAX_LOG_LINES = 5000
ACTIVE_STATES = (RunState.RUNNING, RunState.CONNECTING, RunState.WAITING)


class Bridge(QObject):
    """Carries updates from background threads to the UI thread (queued signals)."""

    log = Signal(str, str)                     # serial, message
    status = Signal(str, dict)                 # serial, status entry
    devices_scanned = Signal(list, dict, str)  # serials, {serial: model}, error
    appium_checked = Signal(bool)
    update_checked = Signal(object, str, bool)  # release or None, error, manual
    run_finished = Signal(list)                # serials


class DeviceRow(QFrame):
    """One phone: status dot, name, script dropdown, Start/Stop."""

    selected = Signal(str)
    start_clicked = Signal(str)
    stop_clicked = Signal(str)

    def __init__(self, serial: str, model: str, parent=None):
        super().__init__(parent)
        self.serial = serial
        self.connected = True
        self.state = RunState.IDLE
        self.setObjectName("DeviceRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self.dot = QLabel()
        self.name = QLabel(model)
        self.name.setObjectName("DeviceName")
        self.detail = QLabel(f"Wi-Fi · {serial}" if is_wireless(serial) else f"USB · {serial}")
        self.detail.setObjectName("Muted")
        self.state_label = QLabel()
        self.state_label.setObjectName("Muted")
        self.state_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.script_combo = QComboBox()
        self.script_combo.setMinimumWidth(200)
        self.script_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("Primary")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("Danger")
        self.start_button.clicked.connect(lambda: self.start_clicked.emit(self.serial))
        self.stop_button.clicked.connect(lambda: self.stop_clicked.emit(self.serial))

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.dot, 0, Qt.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self.name)
        names.addWidget(self.detail)
        top.addLayout(names, 1)
        top.addWidget(self.state_label)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self.script_combo, 1)
        bottom.addWidget(self.start_button)
        bottom.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(bottom)

        self.set_state(RunState.IDLE, "")

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        self.selected.emit(self.serial)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.set_state("disconnected", "Reconnecting over Wi-Fi… (Devices → Wireless Devices)"
                           if is_wireless(self.serial) else "Plug the phone back in to continue")
        else:
            self.set_state(RunState.IDLE, "")

    def set_state(self, state: str, message: str, step: int = 0, total: int = 0,
                  run_number: int = 1, partners: list[str] | None = None) -> None:
        self.state = state
        self.dot.setStyleSheet(status_dot_style(state))
        text = STATUS_LABELS.get(state, state)
        if state == RunState.RUNNING and total:
            text = f"Running {step}/{total}"
        if state == RunState.RUNNING and run_number > 1:
            text += f" · run #{run_number}"
        others = [p for p in (partners or []) if p != self.serial]
        if state in ACTIVE_STATES and others:
            text += f" · with {', '.join(others)}"
        self.state_label.setText(text)
        self.setToolTip(message or text)
        active = state in ACTIVE_STATES
        self.start_button.setEnabled(self.connected and not active and self.script_combo.count() > 0)
        self.stop_button.setEnabled(active)
        self.script_combo.setEnabled(not active)

    def set_scripts(self, entries: list[ScriptEntry]) -> None:
        current = self.script_combo.currentData()
        self.script_combo.blockSignals(True)
        self.script_combo.clear()
        for entry in entries:
            self.script_combo.addItem(entry.display_name, str(entry.path))
        index = self.script_combo.findData(current) if current else -1
        self.script_combo.setCurrentIndex(index if index >= 0 else 0)
        self.script_combo.blockSignals(False)
        self.set_state(self.state, self.toolTip())

    def selected_script_path(self) -> Path | None:
        data = self.script_combo.currentData()
        return Path(data) if data else None


class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager | None = None, scan_devices: bool = True,
                 schedules: ScheduleStore | None = None, appium_watchdog=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        if paths.icon_path().exists():
            self.setWindowIcon(QIcon(str(paths.icon_path())))
        self.resize(1320, 780)

        self.manager = manager or DeviceManager()
        self.schedules = schedules or ScheduleStore()
        self.bridge = Bridge(self)  # parented: destroyed with the window, so late signals are dropped
        self.bridge.log.connect(self._append_log)
        self.bridge.status.connect(self._apply_status)
        self.bridge.devices_scanned.connect(self._apply_scan)
        self.bridge.appium_checked.connect(self._apply_appium_state)
        self.bridge.update_checked.connect(self._apply_update_check)
        self.bridge.run_finished.connect(lambda _: self.history.reload())
        self.manager.add_log_listener(lambda serial, msg: safe_emit(self.bridge.log, serial, msg))
        self.manager.status.subscribe(lambda serial, entry: safe_emit(self.bridge.status, serial, entry))
        self.manager.add_finished_listener(lambda serials, results: safe_emit(self.bridge.run_finished, serials))

        self.rows: dict[str, DeviceRow] = {}
        self.logs: dict[str, list[str]] = {}
        self.selected_serial: str | None = None
        self.scripts: list[ScriptEntry] = []
        self._scan_in_progress = False
        self._last_scan_error = ""
        self.scans_completed = 0
        self._windows: list[QWidget] = []
        self.appium_watchdog = appium_watchdog  # restarts Appium if it stops (None: just report it)
        self._appium_up: bool | None = None
        self.wireless = WirelessStore()
        self.reconnector = AutoReconnector(self.wireless)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self.reload_scripts()

        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(SCAN_INTERVAL_MS)
        self.scan_timer.timeout.connect(self.scan_devices)
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(SCHEDULE_INTERVAL_MS)
        self.schedule_timer.timeout.connect(self.check_schedules)
        if scan_devices:
            self.scan_devices()
            self.scan_timer.start()
            self.schedule_timer.start()
        self._update_schedule_label()

    # ------------------------------------------------------------------ layout

    def _build_actions(self) -> None:
        """One QAction per command, shared by the menu bar (with shortcut hints) and the toolbar."""
        self.actions: dict[str, QAction] = {}

        def action(key: str, text: str, toolbar_text: str, tip: str, slot, shortcut: str | None = None) -> QAction:
            act = QAction(text, self)
            act.setIconText(toolbar_text)
            act.setStatusTip(tip)
            act.setToolTip(tip + (f"  ({QKeySequence(shortcut).toString(QKeySequence.NativeText)})"
                                  if shortcut else ""))
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(lambda _=False: slot())
            self.actions[key] = act
            return act

        action("refresh", "&Refresh Devices", "⟳  Add New Device", "Re-scan USB for connected phones",
               self.scan_devices, "F5")
        action("wireless", "&Wireless Devices…", "📶  Wi-Fi", "Connect phones over Wi-Fi instead of USB",
               self.open_wireless, "Ctrl+Shift+W")
        action("configs", "Open &Configs Folder", "📁  Configs", "Open the folder that holds saved scripts",
               self.open_config_folder)
        action("logs", "Open &Logs Folder", "Logs", "Open the folder with the app's log files", self.open_logs_folder)
        action("new", "&New Script…", "＋  New Script", "Create a script with the form builder or in Python",
               self.new_script, "Ctrl+N")
        action("edit", "&Edit Script…", "✎  Edit Script…", "Open a saved script in the builder", self.edit_script,
               "Ctrl+O")
        action("picker", "Element &Picker…", "◎  Element Picker",
               "Inspect a phone screen, pick elements and record steps", self.open_inspector, "Ctrl+I")
        action("run", "&Run…", "▶  Run…", "Run a script on chosen phones — repeats and cross-phone workflows",
               self.open_run_dialog, "Ctrl+R")
        action("start_all", "Start &All", "▶▶  Start All", "Start each idle phone's selected script",
               self.start_all, "Ctrl+Shift+R")
        action("stop_all", "&Stop All", "■  Stop All", "Stop every running phone", self.stop_all, "Ctrl+.")
        action("schedules", "Sc&hedules…", "⏱  Schedules", "Run scripts automatically at set times",
               self.open_schedules, "Ctrl+Shift+S")
        action("settings", "&Settings…", "⚙  Settings", "Default timeout, log folder, Appium URL, version",
               self.open_settings, "Ctrl+,")
        action("guide", "&User Guide", "Guide", "Open the setup and usage guide", self.open_guide)
        action("updates", "Check for &Updates…", "Updates", "Look for a newer version and install it",
               lambda: self.check_for_updates(manual=True))
        action("about", "&About", "About", "Version information", self.show_about)
        action("quit", "&Quit", "Quit", "Close the app", self.close, "Ctrl+Q")

    def _build_menus(self) -> None:
        a = self.actions
        menus = (
            ("&File", ["new", "edit", None, "configs", "logs", None, "settings", None, "quit"]),
            ("&Devices", ["refresh", "wireless", None, "run", "start_all", "stop_all"]),
            ("&Tools", ["picker", "schedules"]),
            ("&Help", ["guide", "updates", "logs", None, "about"]),
        )
        for title, keys in menus:
            menu = self.menuBar().addMenu(title)
            for key in keys:
                if key is None:
                    menu.addSeparator()
                else:
                    menu.addAction(a[key])

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)
        for key in ("refresh", "wireless", "configs", None, "new", "edit", "picker", None, "run", "start_all", "stop_all",
                    None, "schedules", "settings"):
            if key is None:
                toolbar.addSeparator()
            else:
                toolbar.addAction(self.actions[key])

    def _build_body(self) -> None:
        devices_panel = QFrame()
        devices_panel.setObjectName("Panel")
        devices_layout = QVBoxLayout(devices_panel)
        devices_layout.setContentsMargins(14, 14, 14, 14)
        devices_layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Devices")
        title.setObjectName("PanelTitle")
        self.device_count = QLabel("")
        self.device_count.setObjectName("Muted")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.device_count)
        devices_layout.addLayout(header)

        self.rows_container = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(10)
        self.empty_label = QLabel("No phones found.\n\nConnect a phone with USB debugging enabled,\n"
                                  "accept the prompt on the phone, then click\n“Add New Device”.\n\n"
                                  "No cable? Use “Wi-Fi” to connect over Wi-Fi.")
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.rows_layout.addWidget(self.empty_label)
        self.rows_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rows_container)
        devices_layout.addWidget(scroll, 1)

        # Right: tabs with the live log and the run history
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(8)
        log_header = QHBoxLayout()
        self.log_title = QLabel("Log")
        self.log_title.setObjectName("PanelTitle")
        self.log_subtitle = QLabel("Select a device to see its log")
        self.log_subtitle.setObjectName("Muted")
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear_log)
        save_button = QPushButton("Save…")
        save_button.clicked.connect(self.save_log)
        log_header.addWidget(self.log_title)
        log_header.addWidget(self.log_subtitle, 1)
        log_header.addWidget(clear_button)
        log_header.addWidget(save_button)
        log_layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_LINES)
        log_layout.addWidget(self.log_view, 1)

        self.history = HistoryPanel()
        self.tabs = QTabWidget()
        self.tabs.addTab(log_tab, "Live Log")
        self.tabs.addTab(self.history, "Run History")
        right = QFrame()
        right.setObjectName("Panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(lambda i: self.history.reload() if i == 1 else None)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(devices_panel)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 7)
        splitter.setSizes([500, 720])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 14, 14, 14)
        body_layout.addWidget(splitter)
        self.setCentralWidget(body)

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.schedule_label = QLabel("")
        bar.addPermanentWidget(self.schedule_label)
        self.appium_label = QLabel("Appium: checking…")
        bar.addPermanentWidget(self.appium_label)
        self.appium_timer = QTimer(self)
        self.appium_timer.setInterval(10000)
        self.appium_timer.timeout.connect(self.check_appium)
        self.appium_timer.start()
        self.check_appium()

    # ------------------------------------------------------------------ devices

    def scan_devices(self) -> None:
        """List phones on a background thread so a slow adb never freezes the UI."""
        if self._scan_in_progress:
            return
        self._scan_in_progress = True
        known = set(self.rows)

        def work() -> None:
            try:
                serials = list_connected_devices()
                if self.reconnector.run(serials):
                    serials = list_connected_devices()
                models = {s: device_model(s) for s in serials if s not in known}
                safe_emit(self.bridge.devices_scanned, serials, models, "")
            except DeviceError as exc:
                safe_emit(self.bridge.devices_scanned, [], {}, str(exc))
            except Exception as exc:  # keep polling even if something odd happens
                safe_emit(self.bridge.devices_scanned, [], {}, f"Device scan failed: {exc}")

        threading.Thread(target=work, name="device-scan", daemon=True).start()

    def _apply_scan(self, serials: list, models: dict, error: str) -> None:
        self._scan_in_progress = False
        self.scans_completed += 1
        if error:
            if error != self._last_scan_error:
                log.warning("Device scan failed: %s", error)
            self.statusBar().showMessage(error, 8000)
            serials = []
        self._last_scan_error = error
        present = set(serials)
        for serial in serials:
            row = self.rows.get(serial)
            if row is None:
                self._add_row(serial, models.get(serial, serial))
                log.info("Device connected: %s (%s)", models.get(serial, serial), serial)
                self._append_log(serial, f"Device connected: {models.get(serial, serial)} ({serial})")
            elif not row.connected:
                row.set_connected(True)
                log.info("Device reconnected: %s", serial)
                self._append_log(serial, "Device reconnected")
        for serial, row in self.rows.items():
            if serial not in present and row.connected and not error:
                row.set_connected(False)
                self.manager.device_removed(serial)
                log.warning("Device disconnected: %s", serial)
                self._append_log(serial, "Device disconnected")
        connected = sum(1 for r in self.rows.values() if r.connected)
        self.device_count.setText(f"{connected} connected")
        self.empty_label.setVisible(not self.rows)

    def _add_row(self, serial: str, model: str) -> None:
        row = DeviceRow(serial, model)
        row.set_scripts(self.scripts)
        row.selected.connect(self.select_device)
        row.start_clicked.connect(self.start_device)
        row.stop_clicked.connect(self.manager.stop)
        self.rows[serial] = row
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        if self.selected_serial is None:
            self.select_device(serial)

    def select_device(self, serial: str) -> None:
        self.selected_serial = serial
        for key, row in self.rows.items():
            row.set_selected(key == serial)
        row = self.rows.get(serial)
        self.log_title.setText(f"Log — {row.name.text() if row else serial}")
        self.log_subtitle.setText(serial)
        self.log_view.setPlainText("\n".join(self.logs.get(serial, [])))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def connected_devices(self) -> list[tuple[str, str]]:
        """(serial, display name) for phones currently plugged in."""
        return [(s, r.name.text()) for s, r in self.rows.items() if r.connected]

    def busy_serials(self) -> set[str]:
        return set(self.manager.running_serials())

    # ------------------------------------------------------------------ running

    def _appium_ready(self, quiet: bool = False) -> bool:
        if is_server_running(self.manager.appium_url):
            return True
        if not quiet:
            QMessageBox.warning(self, "Appium is not running",
                                "The Appium server isn't running, so the phones can't be controlled.\n\n"
                                "Start Appium (or restart this app to have it start Appium for you) "
                                "and try again.")
        return False

    def start_device(self, serial: str) -> None:
        row = self.rows.get(serial)
        if row is None or not row.connected:
            return
        path = row.selected_script_path()
        if path is None:
            QMessageBox.information(self, "No script", "Create a script first with “New Script”.")
            return
        try:
            script = load_any_script(path)
        except ScriptError as exc:
            QMessageBox.warning(self, "Script problem", str(exc))
            return
        if needs_role_mapping(script):
            # A cross-phone workflow needs a phone for each role: ask, starting from this phone.
            self.open_run_dialog(path, serial)
            return
        self.run_script(serial, script)

    def run_script(self, serial: str, script, *, repeat: int = 1, delay_seconds: float = 0,
                   trigger: str = "manual", quiet: bool = False) -> bool:
        """Start an already-loaded single-phone script (JSON dict or PythonScript) on one phone."""
        if serial not in self.rows or not self.rows[serial].connected:
            if not quiet:
                QMessageBox.warning(self, "Device not available", f"{serial} is not connected.")
            return False
        if not self._appium_ready(quiet):
            return False
        if not self.manager.start(serial, script, repeat=repeat, delay_seconds=delay_seconds, trigger=trigger):
            self.statusBar().showMessage(f"{serial} is already running a script", 5000)
            return False
        if self.selected_serial is None:
            self.select_device(serial)
        return True

    def run_workflow(self, mapping: dict[str, str], script, *, repeat: int = 1, delay_seconds: float = 0,
                     trigger: str = "manual", quiet: bool = False) -> bool:
        missing = [s for s in mapping.values() if s not in self.rows or not self.rows[s].connected]
        if missing:
            if not quiet:
                QMessageBox.warning(self, "Phone not available", f"Not connected: {', '.join(missing)}")
            return False
        if not self._appium_ready(quiet):
            return False
        try:
            started = self.manager.start_workflow(mapping, script, repeat=repeat, delay_seconds=delay_seconds,
                                                  trigger=trigger)
        except ScriptError as exc:
            if not quiet:
                QMessageBox.warning(self, "Can't start workflow", str(exc))
            return False
        if not started:
            self.statusBar().showMessage("One of those phones is busy", 5000)
            return False
        self.select_device(next(iter(mapping.values())))
        return True

    def execute(self, request: RunRequest, trigger: str = "manual", quiet: bool = False) -> int:
        """Start a RunRequest; returns how many runs started."""
        try:
            script = load_any_script(request.path)
        except ScriptError as exc:
            if not quiet:
                QMessageBox.warning(self, "Script problem", str(exc))
            return 0
        if request.mapping:
            return int(self.run_workflow(request.mapping, script, repeat=request.repeat,
                                         delay_seconds=request.delay_seconds, trigger=trigger, quiet=quiet))
        started = 0
        for serial in request.serials:
            if self.run_script(serial, script, repeat=request.repeat, delay_seconds=request.delay_seconds,
                               trigger=trigger, quiet=quiet or started > 0):
                started += 1
        return started

    def open_run_dialog(self, path: Path | None = None, serial: str | None = None) -> None:
        self.reload_scripts()
        if path is None and serial is None and self.selected_serial in self.rows:
            path = self.rows[self.selected_serial].selected_script_path()
            serial = self.selected_serial
        dialog = RunDialog(self, self.scripts, self.connected_devices(), self.busy_serials(), path, serial)
        if dialog.exec() == QDialog.Accepted and dialog.result_request:
            started = self.execute(dialog.result_request)
            if started:
                self.statusBar().showMessage(f"Started {dialog.result_request.entry.name}", 4000)

    def run_python_source(self, serial: str, source: str, name: str) -> bool:
        """Entry point used by Developer Mode's “Run on this device”."""
        ok = self.run_script(serial, PythonScript(name=name or "developer script", source=source))
        if ok:
            self.select_device(serial)
        return ok

    def start_all(self) -> None:
        started = skipped = 0
        for serial, row in self.rows.items():
            if not row.connected or self.manager.is_running(serial) or not row.selected_script_path():
                continue
            try:
                script = load_any_script(row.selected_script_path())
            except ScriptError as exc:
                self._append_log(serial, f"✖ {exc}")
                continue
            if needs_role_mapping(script):
                self._append_log(serial, f"Skipped “{script_name(script)}”: it's a cross-phone workflow — "
                                         "use Run… to choose its phones")
                skipped += 1
                continue
            if not self.run_script(serial, script):
                return
            started += 1
        note = f" ({skipped} workflow(s) skipped — use Run…)" if skipped else ""
        self.statusBar().showMessage(f"Started {started} device(s){note}", 5000)

    def stop_all(self) -> None:
        self.manager.stop_all()

    def _apply_status(self, serial: str, entry: dict) -> None:
        row = self.rows.get(serial)
        if row is None or not row.connected:
            return
        row.set_state(entry.get("state", RunState.IDLE), entry.get("message", ""),
                      entry.get("step", 0), entry.get("total_steps", 0), entry.get("run_number", 1),
                      entry.get("partners"))

    # ------------------------------------------------------------------ schedules

    def open_schedules(self) -> None:
        from .schedules import SchedulesDialog

        SchedulesDialog(self, self.schedules).exec()
        self._update_schedule_label()

    def check_schedules(self) -> None:
        for schedule in self.schedules.due():
            self.run_schedule(schedule)
        self._update_schedule_label()

    def run_schedule(self, schedule: Schedule) -> int:
        """Start a scheduled run now. Busy or missing phones are skipped and logged."""
        self.schedules.mark_run(schedule)
        trigger = f"schedule: {schedule.name}"
        path = resolve_script(schedule.script)
        try:
            script = load_any_script(path)
        except ScriptError as exc:
            self.statusBar().showMessage(f"Schedule “{schedule.name}”: {exc}", 10000)
            return 0
        targets = list(schedule.roles.values()) or list(schedule.serials)
        if schedule.roles and set(schedule.roles) != set(roles_of(script)):
            self.statusBar().showMessage(f"Schedule “{schedule.name}”: the script's phone roles changed — "
                                         "edit the schedule", 10000)
            return 0

        def unavailable(serial: str) -> str:
            if serial not in self.rows or not self.rows[serial].connected:
                return "phone not connected"
            return "phone is busy" if self.manager.is_running(serial) else ""

        problems = {serial: unavailable(serial) for serial in targets}
        for serial, reason in problems.items():
            if reason:
                self._append_log(serial, f"⏱ Schedule “{schedule.name}” skipped: {reason}")
        options = dict(repeat=schedule.repeat, delay_seconds=schedule.delay_seconds, trigger=trigger, quiet=True)
        started_on: list[str] = []
        if schedule.roles:
            if not any(problems.values()) and self.run_workflow(schedule.roles, script, **options):
                started_on = targets
        else:
            started_on = [s for s in targets if not problems[s] and self.run_script(s, script, **options)]
        for serial in started_on:
            self._append_log(serial, f"⏱ Started by schedule “{schedule.name}”")
        self.statusBar().showMessage(f"Schedule “{schedule.name}”: started on {len(started_on)} phone(s)", 6000)
        return len(started_on)

    def _update_schedule_label(self) -> None:
        now = datetime.now()
        upcoming = [(s.next_run(now), s) for s in self.schedules.schedules]
        upcoming = [(when, s) for when, s in upcoming if when]
        if not upcoming:
            self.schedule_label.setText("")
            return
        when, schedule = min(upcoming, key=lambda pair: pair[0])
        stamp = when.strftime("%H:%M") if when.date() == now.date() else when.strftime("%a %H:%M")
        self.schedule_label.setText(f"⏱ Next: {schedule.name} at {stamp}   ")

    # ------------------------------------------------------------------ logs

    def _append_log(self, serial: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        lines = [f"{stamp}  {part}" for part in message.splitlines() or [""]]
        buffer = self.logs.setdefault(serial, [])
        buffer.extend(lines)
        if len(buffer) > MAX_LOG_LINES:
            del buffer[: len(buffer) - MAX_LOG_LINES]
        if serial == self.selected_serial:
            for line in lines:
                self.log_view.appendPlainText(line)

    def clear_log(self) -> None:
        if self.selected_serial:
            self.logs[self.selected_serial] = []
        self.log_view.clear()

    def save_log(self) -> None:
        if not self.selected_serial:
            return
        default = paths.logs_dir() / f"{self.selected_serial}-{datetime.now():%Y%m%d-%H%M%S}.log"
        target, _ = QFileDialog.getSaveFileName(self, "Save log", str(default), "Log files (*.log *.txt)")
        if target:
            Path(target).write_text("\n".join(self.logs.get(self.selected_serial, [])) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ windows

    def _track(self, window: QWidget) -> QWidget:
        self._windows.append(window)
        window.destroyed.connect(lambda *_: self._windows.remove(window) if window in self._windows else None)
        window.show()
        return window

    def reload_scripts(self) -> None:
        self.scripts = list_scripts()
        for row in self.rows.values():
            row.set_scripts(self.scripts)

    def open_config_folder(self) -> None:
        paths.configs_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.configs_dir())))

    def new_script(self, path: Path | None = None):
        from .script_editor import ScriptEditorWindow

        window = ScriptEditorWindow(self, script_path=path)
        window.saved.connect(lambda _: self.reload_scripts())
        return self._track(window)

    def edit_script(self) -> None:
        target, _ = QFileDialog.getOpenFileName(self, "Open script", str(paths.configs_dir()),
                                                "Scripts (*.json *.py)")
        if target:
            self.new_script(Path(target))

    def open_inspector(self, add_steps: Callable[[list[dict]], None] | None = None,
                       on_pick: Callable[[str, str], None] | None = None, roles: list[str] | None = None):
        from .inspector import InspectorWindow

        if not self.connected_devices():
            QMessageBox.information(self, "No phones", "Connect a phone first to inspect its screen.")
            return None
        if add_steps is None and on_pick is None:
            # Opened from the toolbar: send picked steps to a new builder window.
            editor = self.new_script()
            add_steps = editor.builder.append_steps
        idle = [s for s, _ in self.connected_devices() if not self.manager.is_running(s)]
        serial = self.selected_serial if self.selected_serial in idle else (idle[0] if idle else None)
        window = InspectorWindow(self, add_steps=add_steps, on_pick=on_pick, roles=roles, serial=serial)
        if on_pick:
            # Opened from the (modal) Edit Step dialog, which blocks every other window:
            # the picker must be modal too, on top of it, or it can't be clicked.
            window.setWindowModality(Qt.ApplicationModal)
        return self._track(window)

    def open_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        if SettingsDialog(self, self.manager).exec():
            self.statusBar().showMessage("Settings saved", 4000)
            self.check_appium()

    def open_wireless(self) -> None:
        from .wireless_dialog import WirelessDialog

        WirelessDialog(self, self.wireless, on_change=self.scan_devices).exec()
        self.scan_devices()

    def open_logs_folder(self) -> None:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir())))

    def open_guide(self) -> None:
        for candidate in (paths.app_dir() / "README.txt", paths.app_dir() / "packaging" / "README_FOR_USERS.txt",
                          paths.app_dir() / "README.md"):
            if candidate.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(candidate)))
                return

    # ------------------------------------------------------------------ updates

    def check_for_updates(self, manual: bool = False) -> None:
        """Ask GitHub for a newer release on a background thread; offer it when there is one."""
        from core import updater

        def work() -> None:
            try:
                safe_emit(self.bridge.update_checked, updater.check(), "", manual)
            except updater.UpdateError as exc:
                safe_emit(self.bridge.update_checked, None, str(exc), manual)
            except Exception as exc:  # never let an update check disturb the app
                log.exception("Update check failed")
                safe_emit(self.bridge.update_checked, None, f"Update check failed: {exc}", manual)

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _apply_update_check(self, release, error: str, manual: bool) -> None:
        from .update_dialog import UpdateDialog, show_check_result

        def open_dialog(found) -> None:
            UpdateDialog(self, found, busy=lambda: bool(self.manager.running_serials()),
                         quit_app=self._quit_for_update).exec()

        show_check_result(self, release, error, manual, open_dialog)

    def _quit_for_update(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.close()
        QApplication.quit()

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<h3>{APP_NAME}</h3><p>Version {APP_VERSION}</p>"
                          "<p>Drive several Android phones at once with Appium: scripts, cross-phone "
                          "workflows, an element picker, schedules and run reports.</p>"
                          f"<p>Data folder: {paths.data_dir()}</p>")

    # ------------------------------------------------------------------ appium

    def check_appium(self) -> None:
        url = self.manager.appium_url
        threading.Thread(target=lambda: safe_emit(self.bridge.appium_checked, is_server_running(url)),
                         daemon=True).start()

    def _apply_appium_state(self, running: bool) -> None:
        was_up, self._appium_up = self._appium_up, running
        if running:
            self.appium_label.setText(f"●  Appium running at {self.manager.appium_url}")
            self.appium_label.setStyleSheet("color: #22c55e;")
            if was_up is False:
                log.info("Appium server is answering again")
            return
        if was_up:
            # Sessions on a stopped server are dead: forget them so the next use reconnects.
            log.warning("Appium server stopped answering at %s", self.manager.appium_url)
            self.manager.drop_all_sessions()
        watchdog = self.appium_watchdog
        if watchdog is not None and watchdog.can_restart():
            self.appium_label.setText("●  Appium stopped — restarting it…")
            self.appium_label.setStyleSheet("color: #f59e0b;")
            if watchdog.due():
                def restart() -> None:
                    watchdog.restart()
                    safe_emit(self.bridge.appium_checked, is_server_running(self.manager.appium_url))

                threading.Thread(target=restart, name="appium-restart", daemon=True).start()
        else:
            self.appium_label.setText("●  Appium not running")
            self.appium_label.setStyleSheet("color: #ef4444;")

    # ------------------------------------------------------------------ shutdown

    def closeEvent(self, event):  # noqa: N802 - Qt override
        running = self.manager.running_serials()
        if running:
            answer = QMessageBox.question(
                self, "Scripts are running",
                f"{len(running)} device(s) are still running a script. Stop them and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        for timer in (self.scan_timer, self.schedule_timer, self.appium_timer):
            timer.stop()
        for window in list(self._windows):
            window.close()
        self.manager.shutdown()
        super().closeEvent(event)
