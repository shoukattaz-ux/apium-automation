"""Helpers for talking to Qt from worker threads safely."""

from __future__ import annotations

from typing import Any


def safe_emit(signal: Any, *args: Any) -> None:
    """Emit a signal from a worker thread, ignoring receivers that have already been destroyed.

    Background work (device scans, screenshots, adb calls) can finish after its
    window was closed; the signal's owner is then gone and PySide raises
    RuntimeError. There is nobody left to tell, so that is not an error.
    """
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


def close_all_windows(app: Any) -> None:
    """Close every top-level window and let Qt finish deleting them.

    Windows closed with WA_DeleteOnClose are only *scheduled* for deletion.
    If no event loop runs afterwards (end of the app, end of a test), PySide's
    exit cleanup and Qt's pending delete both destroy the same object and the
    process crashes on exit. Flushing the deferred deletes here prevents that.
    """
    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in list(app.topLevelWidgets()):
        try:
            widget.close()
        except RuntimeError:  # already deleted
            pass
    for _ in range(3):  # deleting a window can schedule deletes of its children
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
