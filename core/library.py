"""The saved-script library: JSON scripts in /configs and Python scripts in /configs/scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class ScriptEntry:
    path: Path
    name: str
    kind: str  # "json" or "python"

    @property
    def display_name(self) -> str:
        return f"{self.name}  [Developer Script]" if self.kind == "python" else self.name


def list_scripts(configs: Path | None = None) -> list[ScriptEntry]:
    """All saved scripts, JSON first, each group sorted by name."""
    configs = configs or paths.configs_dir()
    entries: list[ScriptEntry] = []
    for path in sorted(configs.glob("*.json")):
        try:
            name = json.loads(path.read_text(encoding="utf-8")).get("name") or path.stem
        except (OSError, ValueError, AttributeError):
            name = f"{path.stem} (unreadable)"
        entries.append(ScriptEntry(path, str(name), "json"))
    entries.sort(key=lambda e: e.name.lower())
    python = [ScriptEntry(p, p.stem, "python") for p in sorted((configs / "scripts").glob("*.py"))]
    return entries + python
