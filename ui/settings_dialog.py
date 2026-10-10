"""Settings dialog: default timeout, Appium URL, log folder, and version info."""

from __future__ import annotations

import platform
import sys
from importlib import metadata
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from core import paths, settings as app_settings
from core.devices import DEFAULT_APPIUM_URL
from core.logging_setup import current_log_file, setup_logging
from core.version import APP_NAME, APP_VERSION


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "?"


def _open(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class SettingsDialog(QDialog):
    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.manager = manager
        self.original = app_settings.current()

        # ---- automation
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(app_settings.MIN_TIMEOUT, app_settings.MAX_TIMEOUT)
        self.timeout.setDecimals(1)
        self.timeout.setSingleStep(1)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(self.original.default_timeout_seconds)
        self.timeout.setToolTip("How long a step waits for an element when the step doesn't set its own timeout")
        self.appium_url = QLineEdit(self.original.appium_url)
        self.appium_url.setPlaceholderText(DEFAULT_APPIUM_URL)
        automation = QGroupBox("Automation")
        automation_form = QFormLayout(automation)
        automation_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        automation_form.addRow("Default element timeout", self.timeout)
        hint = QLabel("Used by Click, Copy, Paste and Wait steps that don't set their own timeout, "
                      "and as the default for new steps.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        automation_form.addRow("", hint)
        automation_form.addRow("Appium server URL", self.appium_url)

        # ---- logs
        self.log_dir = QLineEdit(str(self.original.effective_log_dir()))
        self.log_dir.setReadOnly(True)
        change = QPushButton("Change…")
        change.clicked.connect(self._choose_log_dir)
        reset = QPushButton("Default")
        reset.setToolTip(str(paths.default_logs_dir()))
        reset.clicked.connect(lambda: self.log_dir.setText(str(paths.default_logs_dir())))
        open_logs = QPushButton("Open Logs Folder")
        open_logs.setObjectName("Primary")
        open_logs.clicked.connect(lambda: _open(Path(self.log_dir.text())))
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.log_dir, 1)
        folder_row.addWidget(change)
        folder_row.addWidget(reset)
        current = current_log_file()
        self.current_log = QLabel(f"Current log file: {current.name}" if current else "Logging to console only")
        self.current_log.setObjectName("Muted")
        logs = QGroupBox("Logs")
        logs_form = QFormLayout(logs)
        logs_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        logs_form.addRow("Log folder", folder_row)
        open_row = QHBoxLayout()
        open_row.addWidget(self.current_log, 1)
        open_row.addWidget(open_logs)
        logs_form.addRow("", open_row)

        # ---- about
        about = QGroupBox("About")
        about_form = QFormLayout(about)
        about_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        version = QLabel(f"<b>{APP_NAME}</b> &nbsp; version {APP_VERSION}")
        version.setTextFormat(Qt.RichText)
        about_form.addRow("App", version)
        self.check_updates = QCheckBox("Check for updates when the app starts")
        self.check_updates.setChecked(self.original.check_updates)
        self.check_updates.setToolTip("Looks for a newer release on GitHub and offers to install it. "
                                      "You can also use Help → Check for Updates at any time.")
        about_form.addRow("Updates", self.check_updates)
        about_form.addRow("Components", QLabel(
            f"Python {platform.python_version()} · PySide6 {_version('PySide6')} · "
            f"Appium client {_version('Appium-Python-Client')}"))
        data_row = QHBoxLayout()
        data_label = QLineEdit(str(paths.data_dir()))
        data_label.setReadOnly(True)
        data_label.setCursorPosition(0)
        open_data = QPushButton("Open")
        open_data.clicked.connect(lambda: _open(paths.data_dir()))
        data_row.addWidget(data_label, 1)
        data_row.addWidget(open_data)
        about_form.addRow("Data folder", data_row)
        if getattr(sys, "frozen", False):
            about_form.addRow("Installed in", QLabel(str(paths.app_dir())))

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(automation)
        layout.addWidget(logs)
        layout.addWidget(about)
        layout.addWidget(buttons)

    def _choose_log_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose log folder", self.log_dir.text())
        if folder:
            self.log_dir.setText(folder)

    def result_settings(self) -> app_settings.Settings:
        chosen = Path(self.log_dir.text())
        log_dir = "" if chosen == paths.default_logs_dir() else str(chosen)
        return app_settings.Settings(default_timeout_seconds=self.timeout.value(), log_dir=log_dir,
                                     appium_url=self.appium_url.text().strip(),
                                     check_updates=self.check_updates.isChecked(),
                                     skipped_version=self.original.skipped_version)

    def _save(self) -> None:
        new = self.result_settings()
        old_log_dir = self.original.effective_log_dir()
        app_settings.save(new)
        if new.effective_log_dir() != old_log_dir:
            setup_logging(new.effective_log_dir())
        if self.manager is not None:
            self.manager.appium_url = new.appium_url or DEFAULT_APPIUM_URL
        self.accept()
