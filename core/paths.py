"""Filesystem locations used by the app.

* ``app_dir()`` — where the program lives: the project root from source, or
  the folder holding the .exe when frozen by PyInstaller. Bundled tools
  (``appium-server``, ``platform-tools``) are looked up here.
* ``data_dir()`` — where user data lives (``configs``, ``logs``, ``runs``,
  ``settings.json``). It is ``app_dir()`` whenever that folder is writable, so
  a portable folder keeps everything together and users can drop new scripts
  next to the .exe. If the app was installed somewhere read-only (e.g.
  Program Files) it falls back to ``%LOCALAPPDATA%\\DeviceAutomation``.
* ``resource_dir()`` — read-only bundled resources (icons, default configs).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .version import APP_ID

_logs_override: Path | None = None


def is_frozen() -> bool:
    """True when running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory the program runs from."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Directory that holds read-only bundled resources (icons, default configs)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent.parent


@lru_cache(maxsize=None)
def _is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
        return True
    except OSError:
        return False


def user_data_fallback() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_ID


def data_dir() -> Path:
    """Directory that holds user-editable data (configs, logs, run history, settings)."""
    folder = app_dir()
    if is_frozen() and not _is_writable(folder):
        return user_data_fallback()
    return folder


def configs_dir() -> Path:
    """Folder containing JSON step scripts."""
    return data_dir() / "configs"


def python_scripts_dir() -> Path:
    """Folder containing raw Python developer scripts."""
    return configs_dir() / "scripts"


def default_logs_dir() -> Path:
    return data_dir() / "logs"


def logs_dir() -> Path:
    """Log folder: the one chosen in Settings, else ``<data>/logs``."""
    return _logs_override or default_logs_dir()


def set_logs_dir(folder: Path | None) -> None:
    global _logs_override
    _logs_override = Path(folder) if folder else None


def runs_dir() -> Path:
    """Run history: one folder per run with its report and screenshots."""
    return data_dir() / "runs"


def schedules_file() -> Path:
    return configs_dir() / "schedules.json"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def icon_path() -> Path:
    """Window/taskbar icon: the multi-size .ico on Windows, PNG elsewhere."""
    assets = resource_dir() / "assets"
    ico = assets / "icon.ico"
    return ico if sys.platform == "win32" and ico.exists() else assets / "icon.png"


def logo_path() -> Path:
    return resource_dir() / "assets" / "icon.png"


def ensure_user_dirs() -> None:
    """Create the data folders, seeding configs with the bundled examples on first run."""
    target = configs_dir()
    if is_frozen() and not target.exists():
        bundled = resource_dir() / "default_configs"
        if bundled.exists():
            shutil.copytree(bundled, target, ignore=shutil.ignore_patterns("schedules.json"))
    python_scripts_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
