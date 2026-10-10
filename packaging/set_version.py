"""Stamp a release version into core/version.py: 1.2.0 -> 1.2.<build number>.

Used by CI on every push to main, so each release is newer than the one before and
installed copies offer it as an update. The major.minor part stays as committed.

    python packaging/set_version.py 57
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "core" / "version.py"
_PATTERN = re.compile(r'APP_VERSION = "(\d+)\.(\d+)(?:\.\d+)*"')


def stamp(build: int, path: Path = VERSION_FILE) -> str:
    text = path.read_text(encoding="utf-8")
    match = _PATTERN.search(text)
    if not match or not 0 < build < 65536:  # Windows version fields are 16-bit
        raise SystemExit(f"Can't stamp build {build} into {path}")
    version = f"{match[1]}.{match[2]}.{build}"
    path.write_text(_PATTERN.sub(f'APP_VERSION = "{version}"', text, count=1), encoding="utf-8")
    return version


if __name__ == "__main__":
    print(stamp(int(sys.argv[1])))
