"""Settings dialog: default timeout, Appium URL, alarm sound, log folder, and version info."""

from __future__ import annotations

import dataclasses
import platform
import sys
from importlib import metadata
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
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
        self.resize(620, 760)  # scrolls on smaller screens
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

        # ---- reliability
        o = self.original
        self.verify_clicks = QCheckBox("Check the app reacted to each click (tap once more if it plainly didn't)")
        self.verify_clicks.setChecked(o.verify_clicks)
        self.verify_clicks.setToolTip("After a click the screen must change (layout or picture). If nothing changed "
                                      "at all, the tap is repeated once. A step can be set to fail instead.")
        self.dismiss_popups = QCheckBox("Close system pop-ups that block a step")
        self.dismiss_popups.setChecked(o.dismiss_popups)
        self.dismiss_popups.setToolTip("“App isn't responding” (presses Wait), “keeps stopping”, system notices "
                                       "(OK / Got it / Not now) and an open notification panel. Only when the "
                                       "step can't find its element.")
        self.popup_permissions = QComboBox()
        for label, value in (("Leave them (don't touch)", "leave"), ("Allow (while using the app)", "allow"),
                             ("Don't allow", "deny")):
            self.popup_permissions.addItem(label, value)
        self.popup_permissions.setCurrentIndex(max(0, self.popup_permissions.findData(o.popup_permissions)))
        self.popup_buttons = QLineEdit(o.popup_buttons)
        self.popup_buttons.setPlaceholderText("e.g. Not now, Skip, No thanks")
        self.popup_buttons.setToolTip("Buttons (exact text, comma separated) to press in any app when they "
                                      "block a step — e.g. an app's own rating or update prompt")
        self.auto_recover = QCheckBox("Reconnect a phone that drops mid-run and repeat the step")
        self.auto_recover.setChecked(o.auto_recover)
        self.auto_recover.setToolTip("Waits up to 2 minutes for the phone: reconnects Wi-Fi phones, revives "
                                     "offline ones and restarts adb if it stopped answering")
        self.silent_typing = QCheckBox("Silent typing: fill fields without the keyboard (any language, emoji)")
        self.silent_typing.setChecked(o.silent_typing)
        self.silent_typing.setToolTip("Sets the text directly and checks it took; falls back to pasting, then to "
                                      "normal typing, on fields that ignore it")
        reliability_box = QGroupBox("Reliability")
        reliability_form = QFormLayout(reliability_box)
        reliability_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        reliability_form.addRow("Clicks", self.verify_clicks)
        reliability_form.addRow("Pop-ups", self.dismiss_popups)
        reliability_form.addRow("Permission prompts", self.popup_permissions)
        reliability_form.addRow("Also press", self.popup_buttons)
        reliability_form.addRow("Connection", self.auto_recover)
        reliability_form.addRow("Typing", self.silent_typing)

        # ---- alerts
        self.alert_enabled = QCheckBox("Ring an alarm when a script still fails after its retries")
        self.alert_enabled.setChecked(self.original.alert_enabled)
        self.alert_sound = QLineEdit(self.original.alert_sound)
        self.alert_sound.setPlaceholderText("Built-in ringtone")
        self.alert_sound.setToolTip("A .wav file to play instead of the built-in ringtone")
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._choose_sound)
        builtin = QPushButton("Built-in")
        builtin.clicked.connect(self.alert_sound.clear)
        sound_row = QHBoxLayout()
        sound_row.addWidget(self.alert_sound, 1)
        sound_row.addWidget(browse)
        sound_row.addWidget(builtin)
        self.alert_repeats = QSpinBox()
        self.alert_repeats.setRange(1, 50)
        self.alert_repeats.setSuffix(" time(s)")
        self.alert_repeats.setValue(max(1, self.original.alert_repeats))
        self.alert_repeats.setToolTip("How many times the sound plays; the 🔔 Stop alarm button silences it early")
        test = QPushButton("▶  Test sound")
        test.clicked.connect(self._test_sound)
        repeat_row = QHBoxLayout()
        repeat_row.addWidget(self.alert_repeats)
        repeat_row.addStretch(1)
        repeat_row.addWidget(test)
        alerts = QGroupBox("Alerts")
        alerts_form = QFormLayout(alerts)
        alerts_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        alerts_form.addRow("Alarm", self.alert_enabled)
        alerts_form.addRow("Sound", sound_row)
        alerts_form.addRow("Play", repeat_row)
        alert_hint = QLabel("Every failure is also written to the Errors tab, with a screenshot.")
        alert_hint.setObjectName("Muted")
        alert_hint.setWordWrap(True)
        alerts_form.addRow("", alert_hint)

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

        content = QWidget()
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 0, 0)
        for section in (automation, reliability_box, alerts, logs, about):
            sections.addWidget(section)
        sections.addStretch(1)
        scroll = QScrollArea()  # small screens: scroll instead of hiding the Save button
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    def _choose_sound(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose alarm sound", self.alert_sound.text(),
                                                "WAV sounds (*.wav)")
        if chosen:
            self.alert_sound.setText(chosen)

    def _test_sound(self) -> None:
        alerter = getattr(self.manager, "alerter", None)
        if alerter is None:
            from core.alerts import Alerter

            alerter = self._test_alerter = Alerter()
        if alerter.ringing:
            alerter.stop()
            return
        alerter.alert("Test sound", force=True, sound=self.alert_sound.text().strip(),
                      repeats=min(self.alert_repeats.value(), 2))

    def _choose_log_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose log folder", self.log_dir.text())
        if folder:
            self.log_dir.setText(folder)

    def result_settings(self) -> app_settings.Settings:
        chosen = Path(self.log_dir.text())
        log_dir = "" if chosen == paths.default_logs_dir() else str(chosen)
        return dataclasses.replace(self.original, default_timeout_seconds=self.timeout.value(), log_dir=log_dir,
                                   appium_url=self.appium_url.text().strip(),
                                   check_updates=self.check_updates.isChecked(),
                                   alert_enabled=self.alert_enabled.isChecked(),
                                   alert_sound=self.alert_sound.text().strip(),
                                   alert_repeats=self.alert_repeats.value(),
                                   verify_clicks=self.verify_clicks.isChecked(),
                                   dismiss_popups=self.dismiss_popups.isChecked(),
                                   popup_permissions=self.popup_permissions.currentData(),
                                   popup_buttons=self.popup_buttons.text().strip(),
                                   auto_recover=self.auto_recover.isChecked(),
                                   silent_typing=self.silent_typing.isChecked())

    def _save(self) -> None:
        new = self.result_settings()
        old_log_dir = self.original.effective_log_dir()
        app_settings.save(new)
        if new.effective_log_dir() != old_log_dir:
            setup_logging(new.effective_log_dir())
        if self.manager is not None:
            self.manager.appium_url = new.appium_url or DEFAULT_APPIUM_URL
        alerter = getattr(self.manager, "alerter", None)
        if alerter is not None and not new.alert_enabled:
            alerter.stop()
        self.accept()
