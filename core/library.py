"""The saved-script library: JSON scripts in /configs and Python scripts in /configs/scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .schema import script_roles


@dataclass(frozen=True)
class ScriptEntry:
    path: Path
    name: str
    kind: str  # "json" or "python"
    roles: tuple[str, ...] = field(default=())

    @property
    def is_workflow(self) -> bool:
        return len(self.roles) >= 2

    @property
    def display_name(self) -> str:
        if self.kind == "python":
            return f"{self.name}  [Developer Script]"
        if self.is_workflow:
            return f"{self.name}  [{' + '.join(self.roles)} workflow]"
        return self.name

    def relative_name(self, configs: Path | None = None) -> str:
        """Path relative to configs/ (how schedules refer to scripts)."""
        try:
            return self.path.relative_to(configs or paths.configs_dir()).as_posix()
        except ValueError:
            return str(self.path)


def list_scripts(configs: Path | None = None) -> list[ScriptEntry]:
    """All saved scripts, JSON first, each group sorted by name."""
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
    return entries + python


def resolve_script(relative: str, configs: Path | None = None) -> Path:
    return (configs or paths.configs_dir()) / relative
