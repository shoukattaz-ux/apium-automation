"""Updating an installed copy from the project's GitHub Releases.

Every push to main publishes a release (see .github/workflows/build.yml) holding the
installer and its SHA-256 checksum. The app asks GitHub's API for the latest release,
offers it when it is newer, downloads the installer, checks it against the checksum,
then runs it silently and exits; the installer replaces the program files (scripts,
pictures, schedules and settings are kept) and starts the app again.

No Qt here, so it is unit tested directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths
from .version import APP_VERSION, INSTALLER_ASSET, UPDATE_REPO

log = logging.getLogger(__name__)

# Every release carries latest.json; release downloads aren't rate limited, unlike GitHub's API
# (60 unauthenticated requests an hour per internet connection, often shared by many people).
MANIFEST_URL = "https://github.com/{repo}/releases/latest/download/latest.json"
DOWNLOAD_URL = "https://github.com/{repo}/releases/download/v{version}/{asset}"
PAGE_URL = "https://github.com/{repo}/releases/tag/v{version}"
API_URL = "https://api.github.com/repos/{repo}/releases/latest"       # releases made before latest.json
NOTES_URL = "https://api.github.com/repos/{repo}/releases/tags/v{version}"  # release notes (optional)
TIMEOUT_SECONDS = 15
CHUNK = 256 * 1024


class UpdateError(RuntimeError):
    """Checking, downloading or verifying an update failed (the message is shown to the user)."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    page_url: str
    installer_url: str
    checksum_url: str
    size: int = 0


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.57' → (1, 2, 57). Anything after the numbers (e.g. '-beta') is ignored."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def is_newer(candidate: str, current: str | None = None) -> bool:
    a, b = parse_version(candidate), parse_version(APP_VERSION if current is None else current)
    length = max(len(a), len(b))
    return a + (0,) * (length - len(a)) > b + (0,) * (length - len(b))


def _open(url: str, accept: str = "application/octet-stream"):
    request = urllib.request.Request(url, headers={
        "User-Agent": f"DeviceAutomation/{APP_VERSION}", "Accept": accept})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)  # noqa: S310 - fixed https URLs


def parse_release(data: dict) -> Release | None:
    """The parts of a GitHub release we need, or None if it has no installer."""
    assets = {asset.get("name"): asset for asset in data.get("assets") or []}
    installer, checksum = assets.get(INSTALLER_ASSET), assets.get(INSTALLER_ASSET + ".sha256")
    if not installer or not checksum or data.get("draft") or data.get("prerelease"):
        return None
    return Release(version=str(data.get("tag_name", "")).lstrip("v"), notes=str(data.get("body") or ""),
                   page_url=str(data.get("html_url") or ""),
                   installer_url=installer["browser_download_url"],
                   checksum_url=checksum["browser_download_url"], size=int(installer.get("size") or 0))


def _get_json(url: str, accept: str = "application/json") -> dict:
    with _open(url, accept=accept) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_problem(exc: urllib.error.HTTPError) -> UpdateError:
    if exc.code in (403, 429):
        return UpdateError("GitHub is limiting update checks from your internet connection right now. "
                           "Try again in an hour, or download the newest version from the Releases page.")
    return UpdateError(f"GitHub answered {exc.code} when checking for updates")


def release_notes(repo: str, version: str) -> str:
    """The release's notes, if GitHub's API answers (it is rate limited, so this is best effort)."""
    try:
        return str(_get_json(NOTES_URL.format(repo=repo, version=version),
                             accept="application/vnd.github+json").get("body") or "")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""


def latest_release(repo: str = UPDATE_REPO) -> Release | None:
    """The newest published release, or None if there is none with an installer yet."""
    try:
        manifest = _get_json(MANIFEST_URL.format(repo=repo))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise _http_problem(exc) from exc
        manifest = None  # a release from before latest.json existed: ask the API
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not reach GitHub to check for updates ({exc})") from exc
    except ValueError as exc:
        raise UpdateError("GitHub sent an unreadable answer") from exc
    if manifest:
        version = str(manifest.get("version") or "").lstrip("v")
        if not parse_version(version):
            raise UpdateError("The latest release's latest.json has no valid version")
        asset = str(manifest.get("installer") or INSTALLER_ASSET)
        return Release(version=version, notes=release_notes(repo, version),
                       page_url=PAGE_URL.format(repo=repo, version=version),
                       installer_url=DOWNLOAD_URL.format(repo=repo, version=version, asset=asset),
                       checksum_url=DOWNLOAD_URL.format(repo=repo, version=version, asset=asset + ".sha256"),
                       size=int(manifest.get("size") or 0))
    try:
        data = _get_json(API_URL.format(repo=repo), accept="application/vnd.github+json")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # no release yet (or the repository was made private)
        raise _http_problem(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not reach GitHub to check for updates ({exc})") from exc
    except ValueError as exc:
        raise UpdateError("GitHub sent an unreadable answer") from exc
    return parse_release(data)


def check(repo: str = UPDATE_REPO) -> Release | None:
    """The latest release if it is newer than this copy, else None."""
    release = latest_release(repo)
    if release and is_newer(release.version):
        log.info("Update available: %s (running %s)", release.version, APP_VERSION)
        return release
    return None


def download(release: Release, folder: Path | None = None,
             progress: Callable[[int, int], None] | None = None) -> Path:
    """Download the installer and verify it against the release's SHA-256 checksum."""
    folder = Path(folder or Path(tempfile.gettempdir()) / "DeviceAutomation-update")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with _open(release.checksum_url) as response:
            expected = response.read().decode("ascii", "replace").split()[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise UpdateError("The release's checksum file is not valid")
        target = folder / f"DeviceAutomation-Setup-{release.version}.exe"
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        with _open(release.installer_url) as response, open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or release.size or 0)
            done = 0
            while chunk := response.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Download failed ({exc})") from exc
    if digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError("The downloaded installer doesn't match the release's checksum; it was not run")
    partial.replace(target)
    log.info("Downloaded and verified %s", target)
    return target


def can_self_update() -> bool:
    """Only the installed Windows app can replace itself (not a copy run from source)."""
    return sys.platform == "win32" and paths.is_frozen()


def installer_arguments() -> list[str]:
    """Silent upgrade of this install, keeping its install mode, then start the app again."""
    args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"]
    program_files = [os.environ.get(name, "") for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    installed = str(paths.app_dir()).lower()
    if any(folder and installed.startswith(folder.lower()) for folder in program_files):
        args.append("/ALLUSERS")  # installed for everyone: Windows asks for admin rights
    else:
        args.append("/CURRENTUSER")
    args.append(f'/DIR={paths.app_dir()}')
    return args


def run_installer(installer: Path) -> None:
    """Start the installer detached; the caller must quit the app right after."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen([str(installer), *installer_arguments()], creationflags=flags, close_fds=True)  # noqa: S603
    log.info("Started installer %s; closing so it can replace the program files", installer)
