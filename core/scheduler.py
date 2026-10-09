"""Scheduled runs, stored in ``configs/schedules.json``.

Two kinds of schedule:

* ``daily`` — at ``time`` ("HH:MM") on the chosen ``weekdays`` (0 = Monday).
  If the app was closed at that time it catches up only within
  ``CATCH_UP_MINUTES``, so opening the app in the evening doesn't fire a
  morning job.
* ``interval`` — every ``every_minutes`` minutes while the app is open.

A schedule targets either a list of phones (single-phone script, each phone
runs it independently) or a ``{role: serial}`` mapping (cross-phone workflow).
The dashboard calls ``due()`` on a timer and starts what it returns.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

CATCH_UP_MINUTES = 15
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Schedule:
    name: str
    script: str                                   # file name inside configs/ (or scripts/x.py)
    kind: str = "daily"                           # "daily" | "interval"
    time: str = "09:00"
    weekdays: list[int] = field(default_factory=lambda: list(range(7)))
    every_minutes: int = 60
    serials: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    repeat: int = 1
    delay_seconds: float = 0
    enabled: bool = True
    last_run: str | None = None                   # ISO timestamp
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def describe(self) -> str:
        if self.kind == "interval":
            when = f"every {self.every_minutes} min"
        else:
            days = ("every day" if sorted(self.weekdays) == list(range(7)) else
                    "weekdays" if sorted(self.weekdays) == list(range(5)) else
                    ", ".join(WEEKDAY_NAMES[d] for d in sorted(self.weekdays)))
            when = f"{self.time}, {days}"
        target = (", ".join(f"{r}={s}" for r, s in self.roles.items()) if self.roles
                  else ", ".join(self.serials) or "no phones")
        repeat = f" × {self.repeat}" if self.repeat != 1 else ""
        return f"{when} → {target}{repeat}"

    def validate(self) -> list[str]:
        problems = []
        if not self.name.strip():
            problems.append("Give the schedule a name")
        if not self.script:
            problems.append("Choose a script")
        if self.kind == "daily":
            try:
                _parse_time(self.time)
            except ValueError:
                problems.append("Time must look like 09:30")
            if not self.weekdays:
                problems.append("Pick at least one day")
        elif self.kind == "interval":
            if int(self.every_minutes) < 1:
                problems.append("Interval must be at least 1 minute")
        else:
            problems.append(f"Unknown schedule type {self.kind!r}")
        if not self.serials and not self.roles:
            problems.append("Choose at least one phone")
        return problems

    def next_run(self, now: datetime) -> datetime | None:
        if not self.enabled:
            return None
        last = _parse_iso(self.last_run)
        if self.kind == "interval":
            return now if last is None else max(now, last + timedelta(minutes=int(self.every_minutes)))
        hour, minute = _parse_time(self.time)
        for offset in range(8):
            day = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day.weekday() in self.weekdays and day >= now - timedelta(minutes=CATCH_UP_MINUTES):
                if last is None or last < day:
                    return day
        return None

    def is_due(self, now: datetime) -> bool:
        upcoming = self.next_run(now)
        return upcoming is not None and upcoming <= now


def _parse_time(text: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in text.strip().split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(text)
    return hour, minute


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class ScheduleStore:
    """Loads/saves schedules and tells the caller which are due."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or paths.schedules_file())
        self._lock = threading.Lock()
        self.schedules: list[Schedule] = []
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = []
        except (OSError, ValueError):
            log.exception("Could not read %s", self.path)
            raw = []
        known = set(Schedule.__dataclass_fields__)
        self.schedules = [Schedule(**{k: v for k, v in item.items() if k in known})
                          for item in raw if isinstance(item, dict)]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(s) for s in self.schedules], indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def upsert(self, schedule: Schedule) -> None:
        for index, existing in enumerate(self.schedules):
            if existing.id == schedule.id:
                self.schedules[index] = schedule
                break
        else:
            self.schedules.append(schedule)
        self.save()

    def remove(self, schedule_id: str) -> None:
        self.schedules = [s for s in self.schedules if s.id != schedule_id]
        self.save()

    def due(self, now: datetime | None = None) -> list[Schedule]:
        now = now or datetime.now()
        return [s for s in self.schedules if s.is_due(now)]

    def mark_run(self, schedule: Schedule, when: datetime | None = None) -> None:
        schedule.last_run = (when or datetime.now()).isoformat(timespec="seconds")
        self.save()
