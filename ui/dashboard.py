"""Phase 3: the main dashboard — device list, per-device script picker, Start/Stop, live logs."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from core import paths
from core.appium_server import is_server_running
from core.devices import DeviceError, device_model, list_connected_devices
from core.library import ScriptEntry, list_scripts
from core.manager import DeviceManager
from core.runner import PythonScript, RunState, load_any_script
from core.schema import ScriptError

from .theme import STATUS_LABELS, status_dot_style

SCAN_INTERVAL_MS = 4000
MAX_LOG_LINES = 5000


class Bridge(QObject):
    """Carries updates from background threads to the UI thread (queued signals)."""

    log = Signal(str, str)                  # serial, message
    status = Signal(str, dict)              # serial, status entry
    devices_scanned = Signal(list, dict, str)  # serials, {serial: model}, error
    appium_checked = Signal(bool)


class DeviceRow(QFrame):
    """One phone: status dot, name, script dropdown, Start/Stop."""

    selected = Signal(str)
    start_clicked = Signal(str)
    stop_clicked = Signal(str)

    def __init__(self, serial: str, model: str, parent=None):
        super().__init__(parent)
        self.serial = serial
        self.connected = True
        self.setObjectName("DeviceRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self.dot = QLabel()
        self.name = QLabel(model)
        self.name.setObjectName("DeviceName")
        self.detail = QLabel(serial)
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
            self.set_state("disconnected", "Plug the phone back in to continue")
        else:
            self.set_state(RunState.IDLE, "")

    def set_state(self, state: str, message: str, step: int = 0, total: int = 0) -> None:
        self.state = state
        self.dot.setStyleSheet(status_dot_style(state))
        text = STATUS_LABELS.get(state, state)
        if state == RunState.RUNNING and total:
            text = f"Running {step}/{total}"
        self.state_label.setText(text)
        self.setToolTip(message or text)
        running = state in (RunState.RUNNING, RunState.CONNECTING)
        self.start_button.setEnabled(self.connected and not running and self.script_combo.count() > 0)
        self.stop_button.setEnabled(running)
        self.script_combo.setEnabled(not running)

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
    def __init__(self, manager: DeviceManager | None = None, scan_devices: bool = True):
        super().__init__()
        self.setWindowTitle("Device Automation Dashboard")
        if paths.icon_path().exists():
            self.setWindowIcon(QIcon(str(paths.icon_path())))
        self.resize(1180, 720)

        self.manager = manager or DeviceManager()
        self.bridge = Bridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.status.connect(self._apply_status)
        self.bridge.devices_scanned.connect(self._apply_scan)
        self.bridge.appium_checked.connect(self._apply_appium_state)
        self.manager.add_log_listener(lambda serial, msg: self.bridge.log.emit(serial, msg))
        self.manager.status.subscribe(lambda serial, entry: self.bridge.status.emit(serial, entry))

        self.rows: dict[str, DeviceRow] = {}
        self.logs: dict[str, list[str]] = {}
        self.selected_serial: str | None = None
        self.scripts: list[ScriptEntry] = []
        self._scan_in_progress = False
        self._editor_windows: list[QWidget] = []

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self.reload_scripts()

        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(SCAN_INTERVAL_MS)
        self.scan_timer.timeout.connect(self.scan_devices)
        if scan_devices:
            self.scan_devices()
            self.scan_timer.start()

    # ------------------------------------------------------------------ layout

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        def action(text: str, tip: str, slot) -> QAction:
            act = QAction(text, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            toolbar.addAction(act)
            return act

        action("⟳  Add New Device", "Re-scan USB for connected phones", self.scan_devices)
        action("📁  Open Config Folder", "Open the folder that holds saved scripts", self.open_config_folder)
        toolbar.addSeparator()
        action("＋  New Script", "Create a script with the form builder or in Python",
               lambda: self.new_script())
        action("✎  Edit Script…", "Open a saved script in the builder", self.edit_script)
        toolbar.addSeparator()
        action("▶  Start All", "Start the selected script on every idle device", self.start_all)
        action("■  Stop All", "Stop every running device", self.stop_all)

    def _build_body(self) -> None:
        # Left: devices
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
                                  "accept the prompt on the phone, then click\n“Add New Device”.")
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.rows_layout.addWidget(self.empty_label)
        self.rows_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rows_container)
        devices_layout.addWidget(scroll, 1)

        # Right: log
        log_panel = QFrame()
        log_panel.setObjectName("Panel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(14, 14, 14, 14)
        log_layout.setSpacing(10)
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

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(devices_panel)
        splitter.addWidget(log_panel)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 6)
        splitter.setSizes([520, 640])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 14, 14, 14)
        body_layout.addWidget(splitter)
        self.setCentralWidget(body)

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
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
                models = {s: device_model(s) for s in serials if s not in known}
                self.bridge.devices_scanned.emit(serials, models, "")
            except DeviceError as exc:
                self.bridge.devices_scanned.emit([], {}, str(exc))
            except Exception as exc:  # keep polling even if something odd happens
                self.bridge.devices_scanned.emit([], {}, f"Device scan failed: {exc}")

        threading.Thread(target=work, name="device-scan", daemon=True).start()

    def _apply_scan(self, serials: list, models: dict, error: str) -> None:
        self._scan_in_progress = False
        if error:
            self.statusBar().showMessage(error, 8000)
            serials = []
        present = set(serials)
        for serial in serials:
            row = self.rows.get(serial)
            if row is None:
                self._add_row(serial, models.get(serial, serial))
                self._log_line(serial, f"Device connected: {models.get(serial, serial)} ({serial})")
            elif not row.connected:
                row.set_connected(True)
                self._log_line(serial, "Device reconnected")
        for serial, row in self.rows.items():
            if serial not in present and row.connected and not error:
                row.set_connected(False)
                self.manager.device_removed(serial)
                self._log_line(serial, "Device disconnected")
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

    # ------------------------------------------------------------------ running

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
        self.run_script(serial, script)

    def run_script(self, serial: str, script) -> bool:
        """Start an already-loaded script (JSON dict or PythonScript) on one device."""
        if serial not in self.rows or not self.rows[serial].connected:
            QMessageBox.warning(self, "Device not available", f"{serial} is not connected.")
            return False
        if not is_server_running(self.manager.appium_url):
            QMessageBox.warning(self, "Appium is not running",
                                "The Appium server isn't running, so the phone can't be controlled.\n\n"
                                "Start Appium (or restart this app to have it start Appium for you) "
                                "and try again.")
            return False
        if not self.manager.start(serial, script):
            self.statusBar().showMessage(f"{serial} is already running a script", 5000)
            return False
        if self.selected_serial is None:
            self.select_device(serial)
        return True

    def run_python_source(self, serial: str, source: str, name: str) -> bool:
        """Entry point used by Developer Mode's “Run on this device”."""
        ok = self.run_script(serial, PythonScript(name=name or "developer script", source=source))
        if ok:
            self.select_device(serial)
        return ok

    def start_all(self) -> None:
        started = 0
        for serial, row in self.rows.items():
            if row.connected and not self.manager.is_running(serial) and row.selected_script_path():
                try:
                    script = load_any_script(row.selected_script_path())
                except ScriptError as exc:
                    self._log_line(serial, f"✖ {exc}")
                    continue
                if not self.run_script(serial, script):
                    return
                started += 1
        self.statusBar().showMessage(f"Started {started} device(s)", 4000)

    def stop_all(self) -> None:
        self.manager.stop_all()

    def _apply_status(self, serial: str, entry: dict) -> None:
        row = self.rows.get(serial)
        if row is None or not row.connected:
            return
        row.set_state(entry.get("state", RunState.IDLE), entry.get("message", ""),
                      entry.get("step", 0), entry.get("total_steps", 0))

    # ------------------------------------------------------------------ logs

    def _log_line(self, serial: str, message: str) -> None:
        self._append_log(serial, message)

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

    # ------------------------------------------------------------------ scripts

    def reload_scripts(self) -> None:
        self.scripts = list_scripts()
        for row in self.rows.values():
            row.set_scripts(self.scripts)

    def open_config_folder(self) -> None:
        paths.configs_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.configs_dir())))

    def new_script(self, path: Path | None = None) -> None:
        from .script_editor import ScriptEditorWindow

        window = ScriptEditorWindow(self, script_path=path)
        window.saved.connect(lambda _: self.reload_scripts())
        window.destroyed.connect(lambda *_: self._editor_windows.remove(window)
                                 if window in self._editor_windows else None)
        self._editor_windows.append(window)
        window.show()

    def edit_script(self) -> None:
        target, _ = QFileDialog.getOpenFileName(self, "Open script", str(paths.configs_dir()),
                                                "Scripts (*.json *.py)")
        if target:
            self.new_script(Path(target))

    # ------------------------------------------------------------------ appium

    def check_appium(self) -> None:
        url = self.manager.appium_url
        threading.Thread(target=lambda: self.bridge.appium_checked.emit(is_server_running(url)),
                         daemon=True).start()

    def _apply_appium_state(self, running: bool) -> None:
        if running:
            self.appium_label.setText(f"●  Appium running at {self.manager.appium_url}")
            self.appium_label.setStyleSheet("color: #22c55e;")
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
        self.scan_timer.stop()
        self.appium_timer.stop()
        self.manager.shutdown()
        super().closeEvent(event)
