"""Filesystem locations used by the app.

When running from source, everything is relative to the project root. When
frozen by PyInstaller, the editable ``configs`` folder lives *next to the
.exe* (so users can drop in new scripts without rebuilding), and read-only
bundled resources live in PyInstaller's extraction directory.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory that holds user-editable data (configs, logs)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Directory that holds read-only bundled resources (icons, default configs)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent.parent


def configs_dir() -> Path:
    """Folder containing JSON step scripts."""
    return app_dir() / "configs"


def python_scripts_dir() -> Path:
    """Folder containing raw Python developer scripts."""
    return configs_dir() / "scripts"


def logs_dir() -> Path:
    return app_dir() / "logs"


def runs_dir() -> Path:
    """Run history: one folder per run with its report and screenshots."""
    return app_dir() / "runs"


def schedules_file() -> Path:
    return configs_dir() / "schedules.json"


def icon_path() -> Path:
    return resource_dir() / "assets" / "icon.png"


def ensure_user_dirs() -> None:
    """Create the configs folders, seeding them with the bundled examples on first run."""
    target = configs_dir()
    if is_frozen() and not target.exists():
        bundled = resource_dir() / "default_configs"
        if bundled.exists():
            shutil.copytree(bundled, target)
    python_scripts_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
