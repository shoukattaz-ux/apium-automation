"""The Errors / Stuck log: every place an automation failed or got stuck, in one list.

Separate from the activity log: one short entry per problem with the phone, script
(and group), the step, the reason and a screenshot of the phone at that moment, so
"where and why did it get stuck?" is answered without reading the whole log.

Entries are JSON lines in ``logs/errors.jsonl`` (append-only, safe to read while
runs add to it). Screenshots stay in their run's report folder; entries point at them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

MAX_ENTRIES_SHOWN = 2000

# Kinds of entry
STEP_SKIPPED = "step skipped"      # a step failed after its retries and was skipped
STEP_FAILED = "step failed"        # a step failed and ended the script (on_fail: stop)
SCRIPT_FAILED = "script failed"    # a script still failed after all its attempts (group)
STUCK = "stuck"                    # a script ran past its time limit and was stopped
CONNECTION = "connection"          # the phone or Appium connection was lost
POPUP = "pop-up closed"            # a system pop-up blocked a step and was closed
NO_REACTION = "no reaction"        # the app showed no change after a click (tapped twice)
RECONNECTED = "reconnected"        # the phone dropped and was reconnected mid-run
APP_RETURNED = "app brought back"  # the run had left the app; it was brought back to the front


@dataclass
class ErrorEntry:
    kind: str
    device: str
    script: str
    reason: str
    step: str = ""                 # e.g. "4  Click text=Video"
    group: str = ""
    attempts: int = 1
    screenshot: str = ""           # absolute path, if one was taken
    report: str = ""               # the run's report folder
    time: float = field(default_factory=time.time)

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time))


def errors_file() -> Path:
    return paths.logs_dir() / "errors.jsonl"


class ErrorLog:
    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self.listeners: list[Callable[[ErrorEntry], None]] = []

    @property
    def path(self) -> Path:
        return self._path or errors_file()

    def add(self, entry: ErrorEntry) -> None:
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as out:
                    out.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        except OSError as exc:  # never let logging an error become one
            log.warning("Could not write the errors log: %s", exc)
        log.warning("[%s] %s: %s — %s%s", entry.device, entry.kind, entry.script,
                    f"step {entry.step}: " if entry.step else "", entry.reason)
        for listener in list(self.listeners):
            try:
                listener(entry)
            except Exception:
                log.exception("Errors-log listener failed")

    def entries(self, limit: int = MAX_ENTRIES_SHOWN) -> list[ErrorEntry]:
        """Newest first."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        known = set(ErrorEntry.__dataclass_fields__)
        result = []
        for line in reversed(lines[-limit:]):
            try:
                data = json.loads(line)
                result.append(ErrorEntry(**{k: v for k, v in data.items() if k in known}))
            except (ValueError, TypeError):
                continue
        return result

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
