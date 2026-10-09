"""User settings, stored as JSON in ``<data>/settings.json``.

    settings = load()           # read (or defaults) and apply
    settings.default_timeout_seconds = 20
    save(settings)              # write and apply

``current()`` returns the settings in effect; the engine reads the default
element timeout from it, and ``paths.logs_dir()`` follows ``log_dir``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

MIN_TIMEOUT, MAX_TIMEOUT = 1.0, 600.0


@dataclass
class Settings:
    default_timeout_seconds: float = 15.0   # used when a step doesn't set its own timeout
    log_dir: str = ""                       # empty = <data>/logs
    appium_url: str = ""                    # empty = APPIUM_URL env var or http://127.0.0.1:4723

    def effective_log_dir(self) -> Path:
        return Path(self.log_dir) if self.log_dir else paths.default_logs_dir()


_current = Settings()


def current() -> Settings:
    return _current


def default_timeout() -> float:
    return float(_current.default_timeout_seconds)


def apply(settings: Settings) -> None:
    global _current
    settings.default_timeout_seconds = min(MAX_TIMEOUT, max(MIN_TIMEOUT, float(settings.default_timeout_seconds)))
    _current = settings
    paths.set_logs_dir(Path(settings.log_dir) if settings.log_dir else None)


def load(path: Path | None = None) -> Settings:
    """Read settings (missing/corrupt file → defaults) and make them current."""
    path = Path(path or paths.settings_file())
    known = {f.name for f in fields(Settings)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        settings = Settings(**{k: v for k, v in raw.items() if k in known})
    except FileNotFoundError:
        settings = Settings()
    except (OSError, ValueError, TypeError):
        log.warning("Could not read %s; using default settings", path, exc_info=True)
        settings = Settings()
    apply(settings)
    return settings


def save(settings: Settings, path: Path | None = None) -> None:
    apply(settings)
    path = Path(path or paths.settings_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    tmp.replace(path)
