"""Structured logging: a rotating, dated log file plus global exception hooks.

* ``logs/app-YYYY-MM-DD.log`` — one file per day the app is started, rotated
  at ``MAX_BYTES`` with ``BACKUP_COUNT`` older parts kept (``.1``, ``.2``…).
  Dated files older than ``KEEP_DAYS`` are deleted at startup.
* A console handler is added only when there is a console (windowed
  PyInstaller builds have none).
* ``install_exception_hooks`` sends every unhandled exception — main thread,
  Qt slots, worker threads — to the log with its full traceback, and lets the
  UI show a friendly dialog for it.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5
KEEP_DAYS = 30

log = logging.getLogger("app")

_file_handler: RotatingFileHandler | None = None
_console_handler: logging.Handler | None = None


def log_file_for(folder: Path, day: datetime | None = None) -> Path:
    return Path(folder) / f"app-{(day or datetime.now()):%Y-%m-%d}.log"


def current_log_file() -> Path | None:
    return Path(_file_handler.baseFilename) if _file_handler else None


def setup_logging(folder: Path, verbose: bool | None = None, console: bool = True) -> Path:
    """(Re)configure the root logger to write to ``folder``. Returns the log file path.

    Called again (e.g. after the log folder changes in Settings) it swaps the
    file handler and keeps the level unless ``verbose`` is given.
    """
    global _file_handler, _console_handler
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    formatter = logging.Formatter(LOG_FORMAT)

    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
    _file_handler = RotatingFileHandler(log_file_for(folder), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                                        encoding="utf-8", delay=False)
    _file_handler.setFormatter(formatter)
    root.addHandler(_file_handler)

    if console and _console_handler is None and sys.stderr is not None:
        _console_handler = logging.StreamHandler()
        _console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(_console_handler)

    if verbose is not None or root.level in (logging.NOTSET, logging.WARNING):
        root.setLevel(logging.DEBUG if verbose else logging.INFO)  # None keeps the current level
    for noisy in ("urllib3", "selenium", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    _prune_old_logs(folder)
    return Path(_file_handler.baseFilename)


def _prune_old_logs(folder: Path) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in folder.glob("app-*.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


ErrorCallback = Callable[[str, BaseException], None]   # (title, exception)


def install_exception_hooks(on_main_thread_error: ErrorCallback | None = None) -> None:
    """Log every unhandled exception; ``on_main_thread_error`` may show it to the user.

    Exceptions raised inside Qt slots reach ``sys.excepthook`` while the app
    keeps running, so the callback can show a non-fatal dialog.
    """
    def excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        if on_main_thread_error is not None:
            try:
                on_main_thread_error("Unexpected error", exc)
            except Exception:  # never let the error reporter itself crash the app
                log.exception("Error dialog failed")

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error("Unhandled exception in thread %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def write_emergency_log(exc: BaseException, folder: Path) -> Path | None:
    """Last resort when logging isn't configured yet: dump the traceback to a crash file."""
    import traceback

    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"crash-{datetime.now():%Y%m%d-%H%M%S}.log"
        target.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
        return target
    except OSError:
        return None
