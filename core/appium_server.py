"""Detect, and optionally start, the local Appium server.

Lookup order for an Appium install the app can start by itself:

1. A bundled server next to the .exe::

       appium-server/node.exe
       appium-server/node_modules/appium/index.js
       appium-server/appium-home/        (UiAutomator2 driver, via APPIUM_HOME)

   (``packaging/prepare_bundle.ps1`` produces this folder), so end users
   never install Node.js or Appium themselves.
2. ``appium`` on PATH (a normal ``npm install -g appium`` install).

If neither exists the dashboard tells the user how to start Appium manually.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .devices import DEFAULT_APPIUM_URL
from .paths import app_dir, logs_dir

log = logging.getLogger(__name__)

# First start can be slow: antivirus scans ~400 Node packages the first time.
START_TIMEOUT_SECONDS = 150

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def is_server_running(url: str = DEFAULT_APPIUM_URL, timeout: float = 2.0) -> bool:
    """True if an Appium server answers ``GET /status`` at ``url``."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return response.status == 200 and "value" in payload
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_appium_command() -> list[str] | None:
    """Command that starts Appium, or None if no install can be found."""
    bundled = app_dir() / "appium-server"
    node = bundled / ("node.exe" if sys.platform == "win32" else "node")
    entry = bundled / "node_modules" / "appium" / "index.js"
    if node.is_file() and entry.is_file():
        return [str(node), str(entry)]
    on_path = shutil.which("appium")
    if on_path:
        return [on_path]
    return None


def server_environment() -> dict[str, str]:
    """Environment for a server we start: point Appium at the bundled drivers and adb."""
    env = dict(os.environ)
    bundled_home = bundled_appium_home()
    if bundled_home.is_dir():
        env["APPIUM_HOME"] = str(bundled_home)
    adb = "adb.exe" if sys.platform == "win32" else "adb"
    if (app_dir() / "platform-tools" / adb).is_file() and not env.get("ANDROID_HOME"):
        # UiAutomator2 finds adb through ANDROID_HOME/platform-tools.
        env["ANDROID_HOME"] = str(app_dir())
    return env


def bundled_appium_home() -> Path:
    return app_dir() / "appium-server" / "appium-home"


def repair_driver_manifest(home: Path) -> bool:
    """Delete Appium's driver list if it points at folders that don't exist here.

    Appium records each driver's absolute install path in
    ``node_modules/.cache/appium/extensions.yaml``. In the bundle that path is
    the build machine's, so on the user's PC the UiAutomator2 driver would fail
    to load. Without the file Appium rebuilds it on start with the real paths.
    Returns True if the file was removed.
    """
    manifest = Path(home) / "node_modules" / ".cache" / "appium" / "extensions.yaml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return False
    for match in re.finditer(r"^\s*installPath:\s*(.+?)\s*$", text, re.MULTILINE):
        install_path = match.group(1).strip().strip("'\"")
        if not Path(install_path).exists():
            try:
                manifest.unlink()
                log.info("Removed stale Appium driver list (pointed at %s); Appium will rebuild it", install_path)
                return True
            except OSError as exc:
                log.warning("Appium driver list points at %s but could not be reset: %s", install_path, exc)
                return False
    return False


class AppiumServer:
    """A server process this app started; stopped again when the app exits."""

    def __init__(self, url: str = DEFAULT_APPIUM_URL):
        self.url = url
        self.process: subprocess.Popen | None = None
        self._log_file = None

    def start(self, wait_seconds: float = START_TIMEOUT_SECONDS,
              on_wait: Callable[[float], None] | None = None) -> bool:
        """Start Appium (or keep waiting for one we already started) until it answers.

        ``on_wait(seconds_elapsed)`` is called about twice a second so a UI can
        stay responsive. The first start on a PC can be slow while antivirus
        scans Appium's files, hence the generous default timeout.
        """
        if is_server_running(self.url):
            return True
        if self.process is not None and self.process.poll() is None:
            return self._wait(wait_seconds, on_wait)  # still starting from last time: don't launch a second one
        command = find_appium_command()
        if command is None:
            return False
        if bundled_appium_home().is_dir():
            repair_driver_manifest(bundled_appium_home())
        parsed = urlparse(self.url)
        command += ["--address", parsed.hostname or "127.0.0.1", "--port", str(parsed.port or 4723)]
        logs_dir().mkdir(parents=True, exist_ok=True)
        self._log_file = open(Path(logs_dir()) / "appium-server.log", "a", encoding="utf-8")
        log.info("Starting Appium: %s", " ".join(command))
        try:
            self.process = subprocess.Popen(command, stdout=self._log_file, stderr=subprocess.STDOUT,
                                            env=server_environment(), creationflags=_NO_WINDOW,
                                            shell=sys.platform == "win32" and command[0].lower().endswith(".cmd"))
        except OSError as exc:
            log.error("Could not start Appium: %s", exc)
            return False
        return self._wait(wait_seconds, on_wait)

    def _wait(self, wait_seconds: float, on_wait: Callable[[float], None] | None) -> bool:
        started = time.monotonic()
        while time.monotonic() - started < wait_seconds:
            if self.process.poll() is not None:
                log.error("Appium exited with code %s (see logs/appium-server.log)", self.process.returncode)
                self.process = None
                return False
            if is_server_running(self.url, timeout=1):
                log.info("Appium answered after %.0fs", time.monotonic() - started)
                return True
            if on_wait:
                on_wait(time.monotonic() - started)
            time.sleep(0.5)
        log.warning("Appium did not answer within %.0fs (it may still be starting)", wait_seconds)
        return False

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if self._log_file:
            self._log_file.close()
            self._log_file = None
