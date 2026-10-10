"""Offering and installing a newer release (the logic is in core/updater.py)."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout,
)

from core import settings as app_settings
from core import updater
from core.version import APP_VERSION

from .qtutil import safe_emit

log = logging.getLogger(__name__)


class _Signals(QObject):
    progress = Signal(int, int)
    downloaded = Signal(str, str)  # installer path, error


class UpdateDialog(QDialog):
    """“Version X is available” with the release notes; downloads, verifies and runs the installer.

    ``busy()`` tells whether scripts are running (no update then); ``quit_app`` closes the
    app once the installer has started, so it can replace the program files.
    """

    def __init__(self, parent, release: updater.Release, busy: Callable[[], bool] = lambda: False,
                 quit_app: Callable[[], None] | None = None):
        super().__init__(parent)
        self.release = release
        self.busy = busy
        self.quit_app = quit_app or (lambda: None)
        self.setWindowTitle("Update available")
        self.setMinimumSize(560, 420)
        self.signals = _Signals(self)
        self.signals.progress.connect(self._show_progress)
        self.signals.downloaded.connect(self._downloaded)

        title = QLabel(f"<h3>Version {release.version} is available</h3>"
                       f"<p>You have version {APP_VERSION}. Your scripts, pictures, schedules and "
                       "settings are kept.</p>")
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown(release.notes.strip() or "_No release notes._")
        self.progress = QProgressBar()
        self.progress.hide()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("Muted")

        self.install_button = QPushButton("Install now" if updater.can_self_update() else "Open download page")
        self.install_button.setObjectName("Primary")
        self.install_button.clicked.connect(self.install)
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        skip = QPushButton("Skip this version")
        skip.setToolTip("Don't offer this version again at startup (Help → Check for Updates still shows it)")
        skip.clicked.connect(self.skip)
        buttons = QHBoxLayout()
        buttons.addWidget(skip)
        buttons.addStretch(1)
        buttons.addWidget(later)
        buttons.addWidget(self.install_button)
        self.buttons = [self.install_button, later, skip]

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(QLabel("What's new:"))
        layout.addWidget(self.notes, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

    def skip(self) -> None:
        current = app_settings.current()
        current.skipped_version = self.release.version
        app_settings.save(current)
        self.reject()

    def install(self) -> None:
        if not updater.can_self_update():
            # Running from source (or not on Windows): just show where to get it.
            QDesktopServices.openUrl(QUrl(self.release.page_url or self.release.installer_url))
            self.accept()
            return
        if self.busy():
            self.status.setText("Scripts are running. Stop them first, then install the update.")
            return
        for button in self.buttons:
            button.setEnabled(False)
        self.progress.show()
        self.progress.setRange(0, 0)
        self.status.setText("Downloading…")

        def work() -> None:
            try:
                path = updater.download(self.release, progress=lambda done, total: safe_emit(
                    self.signals.progress, done, total))
                safe_emit(self.signals.downloaded, str(path), "")
            except updater.UpdateError as exc:
                safe_emit(self.signals.downloaded, "", str(exc))
            except Exception as exc:  # report, never crash the app over an update
                log.exception("Update download failed")
                safe_emit(self.signals.downloaded, "", f"Unexpected error: {exc}")

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _show_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(done * 100 / total))
            self.status.setText(f"Downloading… {done / 1048576:.0f} of {total / 1048576:.0f} MB")

    def _downloaded(self, path: str, error: str) -> None:
        if error:
            self.progress.hide()
            self.status.setText(f"✖ {error}")
            for button in self.buttons:
                button.setEnabled(True)
            return
        if self.busy():  # a schedule may have started a script during the download
            self.status.setText("A script started meanwhile. Stop it, then click Install now again.")
            for button in self.buttons:
                button.setEnabled(True)
            return
        self.status.setText("Verified. Installing — the app will restart by itself…")
        try:
            updater.run_installer(path)
        except OSError as exc:
            self.status.setText(f"✖ Could not start the installer: {exc}")
            for button in self.buttons:
                button.setEnabled(True)
            return
        self.accept()
        self.quit_app()


def show_check_result(parent, release: updater.Release | None, error: str, manual: bool,
                      open_dialog: Callable[[updater.Release], None]) -> None:
    """React to an update check: offer the release, or (for manual checks) say why not."""
    if error:
        log.warning("Update check failed: %s", error)
        if manual:
            QMessageBox.warning(parent, "Check for updates", error)
        return
    if release is None:
        if manual:
            QMessageBox.information(parent, "Check for updates", f"You have the latest version ({APP_VERSION}).")
        return
    if not manual and release.version == app_settings.current().skipped_version:
        log.info("Update %s available but skipped by the user", release.version)
        return
    open_dialog(release)
