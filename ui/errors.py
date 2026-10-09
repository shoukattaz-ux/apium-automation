"""User-facing error reporting: a clean dialog instead of a raw traceback or a silent exit."""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen

from core import paths
from core.logging_setup import current_log_file

_showing = False


def _display(path: Path | None) -> str:
    if path is None:
        return "the logs folder"
    try:
        return str(path.relative_to(paths.data_dir()))
    except ValueError:
        return str(path)


def show_error_dialog(exc: BaseException, *, fatal: bool, title: str = "Something went wrong",
                      log_file: Path | None = None) -> None:
    """Show a friendly error with an 'Open Logs Folder' button; full traceback under Details.

    Re-entrant calls (an error while the dialog is open) are ignored — they are
    already in the log.
    """
    global _showing
    if _showing or QApplication.instance() is None:
        return
    _showing = True
    try:
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QSplashScreen):  # always-on-top: it would hide the dialog
                widget.close()
        log_file = log_file or current_log_file()
        summary = f"{type(exc).__name__}: {exc}".strip()
        box = QMessageBox(QMessageBox.Critical, title, f"Something went wrong — details saved to {_display(log_file)}")
        box.setInformativeText(
            ("The app has to close. " if fatal else "The app can keep running, but the last action didn't finish. ")
            + "If this keeps happening, send that log file to whoever supports this tool.\n\n" + summary[:300])
        box.setDetailedText("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        open_logs = box.addButton("Open Logs Folder", QMessageBox.ActionRole)
        box.addButton("Close" if fatal else "OK", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_logs:
            folder = log_file.parent if log_file else paths.logs_dir()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    finally:
        _showing = False


def ensure_app() -> QApplication:
    """A QApplication for showing a crash dialog even if the crash happened before one existed."""
    app = QApplication.instance()
    if app is None:
        import sys

        app = QApplication(sys.argv)
        from .theme import apply_theme

        apply_theme(app)
    return app
