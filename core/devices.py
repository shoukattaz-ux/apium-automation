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
    """Return serial numbers of Android devices that are connected and authorized.

    A phone reachable over both USB and Wi-Fi is listed once, by its Wi-Fi serial,
    so it can't be started twice and keeps its row when the cable is pulled.
    """
    ready, problems = parse_adb_devices(run_adb("devices"))
    for serial, state in problems.items():
        hint = " (accept the USB debugging prompt on the phone)" if state == "unauthorized" else ""
        log.warning("Device %s is %s%s", serial, state, hint)
    return drop_usb_duplicates(ready)


_hardware_serials: dict[str, str] = {}


def hardware_serial(serial: str) -> str:
    """The phone's own serial number (same over USB and Wi-Fi); cached per adb serial."""
    if serial not in _hardware_serials:
        try:
            _hardware_serials[serial] = run_adb("shell", "getprop", "ro.serialno", serial=serial).strip() or serial
        except DeviceError:
            return serial
    return _hardware_serials[serial]


def drop_usb_duplicates(serials: list[str]) -> list[str]:
    from .wireless import is_wireless

    wireless = [s for s in serials if is_wireless(s)]
    if not wireless:
        return serials
    on_wifi = {hardware_serial(s) for s in wireless}
    return [s for s in serials if is_wireless(s) or s not in on_wifi]


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

    def exists(self, locator_type: str, locator_value: str, timeout_seconds: float = 3) -> bool:
        """True if the element appears within ``timeout_seconds``."""
        try:
            self.find(locator_type, locator_value, timeout_seconds)
            return True
        except ElementNotFound:
            return False

    def first_present(self, locators: list[tuple[str, str]], timeout_seconds: float = 15) -> int | None:
        """Index of the first of ``locators`` that matches an element, or None after the timeout.

        All locators are checked on every pass (about twice a second), in order, so a
        backup locator is found as soon as it matches instead of after the earlier
        ones each use up a full timeout. A malformed locator is reported and skipped.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        usable = list(range(len(locators)))
        while True:
            for index in list(usable):
                if self.should_stop():
                    raise RunStopped()
                locator_type, locator_value = locators[index]
                try:
                    if self.exists(locator_type, locator_value, timeout_seconds=0):
                        return index
                except ElementNotFound:
                    pass
                except Exception as exc:  # invalid selector: no point asking again
                    if "invalid" not in str(exc).lower() and not isinstance(exc, ValueError):
                        raise
                    log.warning("Locator %s=%s is invalid: %s", locator_type, locator_value, _short_error(exc))
                    usable.remove(index)
            if not usable or time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def close_app(self, package: str) -> None:
        """Force-stop an app."""
        try:
            self._require_driver().terminate_app(package)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"Could not close {package}: {_short_error(exc)}") from exc

    def scroll_to_text(self, text: str) -> None:
        """Scroll the first scrollable list until an item containing ``text`` is visible."""
        from appium.webdriver.common.appiumby import AppiumBy

        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        selector = ("new UiScrollable(new UiSelector().scrollable(true))"
                    f'.scrollIntoView(new UiSelector().textContains("{escaped}"))')
        try:
            self._require_driver().find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector)
        except (DeviceError, RunStopped):
            raise
        except Exception as exc:
            raise ElementNotFound(f"Could not scroll to text “{text}”") from exc

    def window_size(self) -> tuple[int, int]:
        size = self._require_driver().get_window_size()
        return int(size["width"]), int(size["height"])

    def tap(self, x: int, y: int) -> None:
        """Tap absolute screen coordinates (for elements without a usable locator)."""
        self._require_driver().tap([(int(x), int(y))])

    def tap_percent(self, x: float, y: float) -> None:
        """Tap a point given as percentages of the screen size."""
        width, height = self.window_size()
        self.tap(width * float(x) / 100, height * float(y) / 100)

    def swipe_percent(self, start_x: float, start_y: float, end_x: float, end_y: float,
                      duration_ms: int = 400) -> None:
        """Swipe between two points given as percentages of the screen size."""
        width, height = self.window_size()
        self._require_driver().swipe(int(width * float(start_x) / 100), int(height * float(start_y) / 100),
                                     int(width * float(end_x) / 100), int(height * float(end_y) / 100),
                                     int(duration_ms))

    def press_key(self, key: str) -> None:
        """Press a hardware/system key by name (see ``KEYCODES``)."""
        if key not in KEYCODES:
            raise DeviceError(f"Unknown key {key!r}")
        self._require_driver().press_keycode(KEYCODES[key])

    def back(self) -> None:
        self.press_key("back")

    def screenshot_png(self) -> bytes:
        return self._require_driver().get_screenshot_as_png()

    def screenshot(self, path: str) -> None:
        with open(path, "wb") as handle:
            handle.write(self.screenshot_png())

    def page_source(self) -> str:
        """The current screen's UI hierarchy as XML (used by the element picker)."""
        return self._require_driver().page_source


# Android key codes for press_key / the "Press Key" step.
KEYCODES = {
    "back": 4, "home": 3, "enter": 66, "recent_apps": 187, "delete": 67,
    "search": 84, "menu": 82, "volume_up": 24, "volume_down": 25,
}


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
