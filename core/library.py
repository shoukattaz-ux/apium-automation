"""The saved-script library: JSON scripts in /configs, Python scripts in /configs/scripts
and script groups in /configs/groups.

Also the library's housekeeping: duplicate, rename and delete. Deleting never destroys
a file: it moves it to ``configs/deleted/`` (with a time stamp), so a mistake can be
undone by moving the file back. Before deleting, ``references_to`` tells the caller
which schedules and groups still use the script.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .groups import is_group_data
from .schema import ScriptError, script_roles

DELETED_FOLDER = "deleted"


@dataclass(frozen=True)
class ScriptEntry:
    path: Path
    name: str
    kind: str  # "json", "python" or "group"
    roles: tuple[str, ...] = field(default=())
    size: int = 0  # scripts in a group

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    @property
    def is_workflow(self) -> bool:
        return len(self.roles) >= 2

    @property
    def display_name(self) -> str:
        if self.kind == "python":
            return f"{self.name}  [Developer Script]"
        if self.is_group:
            return f"{self.name}  [group · {self.size} script{'s' if self.size != 1 else ''}]"
        if self.is_workflow:
            return f"{self.name}  [{' + '.join(self.roles)} workflow]"
        return self.name

    def relative_name(self, configs: Path | None = None) -> str:
        """Path relative to configs/ (how schedules refer to scripts)."""
        try:
            return self.path.relative_to(configs or paths.configs_dir()).as_posix()
        except ValueError:
            return str(self.path)


def list_scripts(configs: Path | None = None, groups: bool = True) -> list[ScriptEntry]:
    """All saved scripts: groups, then JSON, then Python, each sorted by name."""
    configs = configs or paths.configs_dir()
    entries: list[ScriptEntry] = []
    for path in sorted(configs.glob("*.json")):
        if path.name == paths.schedules_file().name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data.get("name") or path.stem
            roles = tuple(script_roles(data))
        except (OSError, ValueError, AttributeError):
            name, roles = f"{path.stem} (unreadable)", ()
        entries.append(ScriptEntry(path, str(name), "json", roles))
    entries.sort(key=lambda e: e.name.lower())
    python = [ScriptEntry(p, p.stem, "python") for p in sorted((configs / "scripts").glob("*.py"))]
    found: list[ScriptEntry] = []
    if groups:
        for path in sorted((configs / "groups").glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not is_group_data(data):
                    continue
                found.append(ScriptEntry(path, str(data.get("name") or path.stem), "group",
                                         size=len(data["scripts"])))
            except (OSError, ValueError, AttributeError):
                found.append(ScriptEntry(path, f"{path.stem} (unreadable)", "group"))
        found.sort(key=lambda e: e.name.lower())
    return found + entries + python


def resolve_script(relative: str, configs: Path | None = None) -> Path:
    return (configs or paths.configs_dir()) / relative


def _relative(path: Path, configs: Path) -> str:
    try:
        return path.relative_to(configs).as_posix()
    except ValueError:
        return str(path)


def _unique(folder: Path, stem: str, suffix: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "script"
    target = folder / f"{stem}{suffix}"
    counter = 2
    while target.exists():
        target = folder / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def _copy_name(name: str, taken: set[str]) -> str:
    base = re.sub(r" \(copy(?: \d+)?\)$", "", name)
    candidate, counter = f"{base} (copy)", 2
    while candidate.lower() in taken:
        candidate = f"{base} (copy {counter})"
        counter += 1
    return candidate


def duplicate_script(path: Path, configs: Path | None = None) -> Path:
    """Copy a script or group next to the original, named "<name> (copy)"; returns the new file."""
    configs = configs or paths.configs_dir()
    path = Path(path)
    if not path.is_file():
        raise ScriptError(f"{path.name} no longer exists")
    taken = {e.name.lower() for e in list_scripts(configs)}
    if path.suffix.lower() == ".py":
        target = _unique(path.parent, f"{path.stem}_copy", ".py")
        shutil.copy2(path, target)
        return target
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScriptError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScriptError(f"{path.name} is not a script")
    data["name"] = _copy_name(str(data.get("name") or path.stem), taken)
    target = _unique(path.parent, f"{path.stem}_copy", ".json")
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def rename_script(path: Path, new_name: str) -> None:
    """Change the name shown for a JSON script or group (the file keeps its name, so
    schedules and groups that use it keep working). Python scripts are named by file."""
    new_name = new_name.strip()
    if not new_name:
        raise ScriptError("The name can't be empty")
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ScriptError("Developer scripts are named after their file; rename the .py file instead")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScriptError(f"Could not read {path.name}: {exc}") from exc
    data["name"] = new_name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def references_to(path: Path, configs: Path | None = None) -> list[str]:
    """Schedules and groups that use this script, as readable lines ("Schedule: Morning")."""
    configs = configs or paths.configs_dir()
    relative = _relative(Path(path), configs)
    found: list[str] = []
    try:
        schedules = json.loads((configs / paths.schedules_file().name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        schedules = []
    for item in schedules if isinstance(schedules, list) else []:
        if isinstance(item, dict) and item.get("script") == relative:
            found.append(f"Schedule: {item.get('name') or '(unnamed)'}")
    for group_file in sorted((configs / "groups").glob("*.json")):
        try:
            data = json.loads(group_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if is_group_data(data) and relative in data["scripts"]:
            found.append(f"Group: {data.get('name') or group_file.stem}")
    return found


def delete_script(path: Path, configs: Path | None = None) -> Path:
    """Move the script (or group) to configs/deleted/; returns where it went."""
    configs = configs or paths.configs_dir()
    path = Path(path)
    if not path.is_file():
        raise ScriptError(f"{path.name} no longer exists")
    trash = configs / DELETED_FOLDER
    trash.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = trash / f"{stamp}-{path.name}"
    counter = 2
    while target.exists():
        target = trash / f"{stamp}-{counter}-{path.name}"
        counter += 1
    shutil.move(str(path), str(target))
    return target

