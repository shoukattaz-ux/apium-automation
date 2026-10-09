"""Phase 1: discover Android phones over adb and open one Appium session per phone.

``DeviceSession`` also exposes the small action API (open_app, click, scroll,
wait_for_element, copy_text, paste_text, wait) that both JSON scripts and
Developer Mode Python scripts use, so the two authoring modes share one engine.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from itertools import count
from typing import Callable

from .paths import app_dir

log = logging.getLogger(__name__)

DEFAULT_APPIUM_URL = os.environ.get("APPIUM_URL", "http://127.0.0.1:4723")
ADB_TIMEOUT_SECONDS = 10

# Each parallel UiAutomator2 session needs its own host-side systemPort.
_system_ports = count(8200)
_system_ports_lock = threading.Lock()


def next_system_port() -> int:
    with _system_ports_lock:
        return next(_system_ports)

# Hide the console window that would otherwise flash up for every adb call on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class DeviceError(RuntimeError):
    """A device could not be reached or an action on it failed."""


class ElementNotFound(DeviceError):
    """No element matched a locator within its timeout."""


class RunStopped(Exception):
    """Raised by an action when the user pressed Stop for this device."""


def adb_path() -> str:
    """Locate adb: $ADB, a bundled platform-tools next to the app, $ANDROID_HOME, then PATH."""
    if os.environ.get("ADB"):
        return os.environ["ADB"]
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    for root in (str(app_dir()), os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")):
        if root:
            candidate = os.path.join(root, "platform-tools", exe)
            if os.path.isfile(candidate):
                return candidate
    return shutil.which("adb") or "adb"


def run_adb(*args: str, serial: str | None = None, timeout: float = ADB_TIMEOUT_SECONDS) -> str:
    """Run an adb command and return stdout. Raises DeviceError on failure."""
    command = [adb_path()]
    if serial:
        command += ["-s", serial]
    command += list(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                creationflags=_NO_WINDOW)
    except FileNotFoundError as exc:
        raise DeviceError("adb was not found. Install Android platform-tools and add it to PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"adb {' '.join(args)} timed out") from exc
    if result.returncode != 0:
        raise DeviceError(result.stderr.strip() or f"adb {' '.join(args)} failed")
    return result.stdout


def parse_adb_devices(output: str) -> tuple[list[str], dict[str, str]]:
    """Split ``adb devices`` output into (ready serials, {serial: problem state})."""
    ready: list[str] = []
    problems: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            ready.append(serial)
        else:
            problems[serial] = state  # "unauthorized", "offline", ...
    return ready, problems


def list_connected_devices() -> list[str]:
    """Return serial numbers of Android devices that are connected and authorized."""
    ready, problems = parse_adb_devices(run_adb("devices"))
    for serial, state in problems.items():
        hint = " (accept the USB debugging prompt on the phone)" if state == "unauthorized" else ""
        log.warning("Device %s is %s%s", serial, state, hint)
    return ready


def device_model(serial: str) -> str:
    """Human-friendly model name such as 'Pixel 7', or the serial if unavailable."""
    try:
        return run_adb("shell", "getprop", "ro.product.model", serial=serial).strip() or serial
    except DeviceError:
        return serial


def list_installed_packages(serial: str, third_party_only: bool = True) -> list[str]:
    """Package IDs installed on a device, sorted."""
    args = ["shell", "pm", "list", "packages"] + (["-3"] if third_party_only else [])
    output = run_adb(*args, serial=serial)
    return sorted(line.split(":", 1)[1].strip() for line in output.splitlines() if line.startswith("package:"))


class DeviceSession:
    """One Appium (UiAutomator2) session bound to one phone.

    The action methods are the public API for Developer Mode scripts::

        def run(device):
            device.open_app("com.example.app")
            device.click("id", "com.example.app:id/button")
            value = device.copy_text("id", "com.example.app:id/title")
            device.paste_text("id", "com.other.app:id/input", value)
    """

    def __init__(self, serial: str, appium_url: str = DEFAULT_APPIUM_URL, connect: bool = True,
                 system_port: int | None = None):
        self.serial = serial
        self.appium_url = appium_url
        self.system_port = system_port or next_system_port()
        self.driver = None
        self._lock = threading.RLock()
        self.should_stop: Callable[[], bool] = lambda: False
        if connect:
            self.connect()

    def __repr__(self) -> str:
        return f"DeviceSession({self.serial!r}, connected={self.is_connected})"

    # ----------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Open the Appium session. Raises DeviceError with a readable message on failure."""
        from appium import webdriver
        from appium.options.android import UiAutomator2Options

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.udid = self.serial
        options.no_reset = True
        options.new_command_timeout = 600
        options.system_port = self.system_port
        try:
            with self._lock:
                self.driver = webdriver.Remote(self.appium_url, options=options)
        except Exception as exc:  # urllib3/selenium raise many different types here
            self.driver = None
            raise DeviceError(f"Could not start Appium session for {self.serial}: {_short_error(exc)}") from exc

    @property
    def is_connected(self) -> bool:
        return self.driver is not None

    def close(self) -> None:
        with self._lock:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:  # session may already be gone
                    pass
                self.driver = None

    def _require_driver(self):
        if self.should_stop():
            raise RunStopped(f"Stopped on {self.serial}")
        if self.driver is None:
            raise DeviceError(f"{self.serial} is not connected")
        return self.driver

    # ----------------------------------------------------------------- queries

    def current_package(self) -> str:
        return self._require_driver().current_package

    def installed_packages(self) -> list[str]:
        return list_installed_packages(self.serial)

    # ----------------------------------------------------------------- actions

    def open_app(self, package: str) -> None:
        """Launch an app (or bring it to the foreground)."""
        try:
            self._require_driver().activate_app(package)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"Could not open {package}: {_short_error(exc)}") from exc

    def find(self, locator_type: str, locator_value: str, timeout_seconds: float = 15):
        """Wait up to ``timeout_seconds`` for an element and return it."""
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._require_driver()
        by, value = to_appium_locator(locator_type, locator_value)
        try:
            return WebDriverWait(driver, timeout_seconds, poll_frequency=0.5).until(
                EC.presence_of_element_located((by, value)))
        except TimeoutException as exc:
            raise ElementNotFound(
                f"Element {locator_type}={locator_value} not found within {timeout_seconds:g}s") from exc

    def wait_for_element(self, locator_type: str, locator_value: str, timeout_seconds: float = 15) -> None:
        self.find(locator_type, locator_value, timeout_seconds)

    def click(self, locator_type: str, locator_value: str, timeout_seconds: float = 15) -> None:
        self.find(locator_type, locator_value, timeout_seconds).click()

    def copy_text(self, locator_type: str, locator_value: str, timeout_seconds: float = 15) -> str:
        """Return the visible text of an element (also placed on the phone's clipboard)."""
        element = self.find(locator_type, locator_value, timeout_seconds)
        text = element.text or element.get_attribute("text") or element.get_attribute("content-desc") or ""
        try:
            self.driver.set_clipboard_text(text)
        except Exception:  # clipboard access is a nice-to-have
            pass
        return text

    def paste_text(self, locator_type: str, locator_value: str, text: str, timeout_seconds: float = 15) -> None:
        """Replace the contents of an input field with ``text``."""
        element = self.find(locator_type, locator_value, timeout_seconds)
        element.click()
        element.clear()
        element.send_keys(text)

    def scroll(self, direction: str = "down", times: int = 1) -> None:
        """Scroll the screen content. ``down`` reveals content further down the page."""
        driver = self._require_driver()
        size = driver.get_window_size()
        area = {
            "left": int(size["width"] * 0.1), "top": int(size["height"] * 0.2),
            "width": int(size["width"] * 0.8), "height": int(size["height"] * 0.6),
        }
        for _ in range(max(1, int(times))):
            if self.should_stop():
                return
            driver.execute_script("mobile: scrollGesture", {**area, "direction": direction, "percent": 0.75})

    def wait(self, seconds: float) -> None:
        """Sleep, waking early if the run is stopped."""
        end = time.monotonic() + float(seconds)
        while not self.should_stop():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def tap(self, x: int, y: int) -> None:
        """Tap absolute screen coordinates (for elements without a usable locator)."""
        self._require_driver().tap([(x, y)])

    def back(self) -> None:
        self._require_driver().back()

    def screenshot(self, path: str) -> None:
        self._require_driver().get_screenshot_as_file(path)


def to_appium_locator(locator_type: str, locator_value: str) -> tuple[str, str]:
    """Map a builder locator type to an Appium ``By`` strategy."""
    from appium.webdriver.common.appiumby import AppiumBy

    kind = locator_type.strip().lower()
    if kind == "text":
        escaped = locator_value.replace("\\", "\\\\").replace('"', '\\"')
        return AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{escaped}")'
    mapping = {
        "id": AppiumBy.ID,
        "xpath": AppiumBy.XPATH,
        "accessibility id": AppiumBy.ACCESSIBILITY_ID,
        "class name": AppiumBy.CLASS_NAME,
        "android uiautomator": AppiumBy.ANDROID_UIAUTOMATOR,
    }
    if kind not in mapping:
        raise DeviceError(f"Unknown locator type {locator_type!r}")
    return mapping[kind], locator_value


def connect_all_devices(appium_url: str = DEFAULT_APPIUM_URL) -> dict[str, DeviceSession]:
    """Open a session on every connected phone. Failures are logged and skipped."""
    sessions: dict[str, DeviceSession] = {}
    try:
        serials = list_connected_devices()
    except DeviceError as exc:
        log.error("%s", exc)
        return sessions
    for serial in serials:
        try:
            sessions[serial] = DeviceSession(serial, appium_url)
            log.info("Connected to %s", serial)
        except DeviceError as exc:
            log.error("%s", exc)
    return sessions


def _short_error(exc: Exception) -> str:
    """First line of an exception message — Appium errors include long stack traces."""
    text = str(exc).strip() or type(exc).__name__
    first = text.splitlines()[0]
    if "Max retries exceeded" in text or "Connection refused" in text or "Failed to establish" in text:
        return "the Appium server is not running (start it with `appium`)"
    return first.removeprefix("Message: ")
