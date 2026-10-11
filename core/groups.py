"""Script groups: several saved scripts run one after another on a phone.

A group is a small JSON file in ``configs/groups/``::

    {
      "name": "Morning routine",
      "scripts": ["open_facebook.json", "post_status.json", "scripts/cleanup.py"],
      "retries": 1,              # extra attempts for a script that fails
      "pause_seconds": 3,        # wait between scripts
      "max_minutes": 10,         # a script running longer is "stuck": stopped, logged, retried/skipped
      "on_script_fail": "skip",  # "skip" = go on with the next script, "stop" = end the group
      "alert": true              # ring the alarm when a script still fails after its retries
    }

Script paths are relative to ``configs/``, like schedules use. Groups can be picked
anywhere a script can (device rows, Run…, Schedules, ``--run``); the run itself is
handled by ``DeviceManager``. Groups contain single-phone scripts (a cross-phone
workflow needs its own phone mapping and can't be one step of a phone's group).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .schema import ScriptError

ON_FAIL = ("skip", "stop")
MAX_RETRIES = 10


@dataclass
class Group:
    name: str
    scripts: list[str] = field(default_factory=list)
    retries: int = 1
    pause_seconds: float = 2.0
    max_minutes: float = 0.0          # 0 = no limit
    on_script_fail: str = "skip"
    alert: bool = True
    retry_partial: bool = True        # a script that skipped steps counts as failed (and is retried)
    path: Path | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("path")
        return data


def groups_dir() -> Path:
    return paths.configs_dir() / "groups"


def is_group_data(data: object) -> bool:
    return isinstance(data, dict) and isinstance(data.get("scripts"), list) and "steps" not in data


def group_from_dict(data: dict, path: Path | None = None) -> Group:
    if not is_group_data(data):
        raise ScriptError("Not a script group (it needs a list of scripts)")
    try:
        group = Group(
            name=str(data.get("name") or (path.stem if path else "Group")),
            scripts=[str(s) for s in data.get("scripts", [])],
            retries=int(data.get("retries", 1)),
            pause_seconds=float(data.get("pause_seconds", 2.0)),
            max_minutes=float(data.get("max_minutes", 0.0)),
            on_script_fail=str(data.get("on_script_fail", "skip")),
            alert=bool(data.get("alert", True)),
            retry_partial=bool(data.get("retry_partial", True)),
            path=path,
        )
    except (TypeError, ValueError) as exc:
        raise ScriptError(f"Group settings are not valid: {exc}") from exc
    problems = validate_group(group, check_files=False)
    if problems:
        raise ScriptError("; ".join(problems))
    return group


def load_group(path: str | Path) -> Group:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScriptError(f"Could not read group {path.name}: {exc}") from exc
    return group_from_dict(data, path)


def validate_group(group: Group, check_files: bool = True, configs: Path | None = None) -> list[str]:
    problems = []
    if not group.name.strip():
        problems.append("Give the group a name")
    if not group.scripts:
        problems.append("Add at least one script to the group")
    if not 0 <= group.retries <= MAX_RETRIES:
        problems.append(f"Retries must be between 0 and {MAX_RETRIES}")
    if group.pause_seconds < 0 or group.max_minutes < 0:
        problems.append("Pause and time limit can't be negative")
    if group.on_script_fail not in ON_FAIL:
        problems.append("When a script fails: choose skip or stop")
    if check_files:
        base = configs or paths.configs_dir()
        for relative in group.scripts:
            target = base / relative
            if not target.is_file():
                problems.append(f"Script not found: {relative}")
            elif target.suffix.lower() == ".json":
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    problems.append(f"Script can't be read: {relative}")
                    continue
                if is_group_data(data):
                    problems.append(f"A group can't contain another group: {relative}")
                else:
                    from .schema import script_roles

                    if len(script_roles(data)) >= 2:
                        problems.append(f"{relative} drives several phones; a group runs on one phone")
    return problems


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_").lower() or "group"


def save_group(group: Group, path: Path | None = None) -> Path:
    """Write the group (to its own file, or a new one named after it); returns the path."""
    problems = validate_group(group)
    if problems:
        raise ScriptError("; ".join(problems))
    folder = groups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = path or group.path
    if target is None:
        target = folder / f"{_slug(group.name)}.json"
        counter = 2
        while target.exists():
            target = folder / f"{_slug(group.name)}_{counter}.json"
            counter += 1
    target.write_text(json.dumps(group.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    group.path = target
    return target
