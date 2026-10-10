"""Connect phones over Wi-Fi with adb instead of a USB cable.

Three ways in, all ending with a serial like ``192.168.1.20:5555`` that the rest
of the app (Appium sessions, scripts, reports) uses like any USB serial:

* **Switch a USB phone to Wi-Fi** – ``adb tcpip 5555`` then ``adb connect``.
  Works on every Android version; needs the cable once, and again after the
  phone restarts.
* **Pair** (Android 11+, Developer options → Wireless debugging → Pair device
  with pairing code) – ``adb pair``, then connect to the address shown on the
  Wireless debugging screen. No cable at all.
* **Connect** to an address directly.

Connected addresses are remembered in ``configs/wireless_devices.json`` so the
dashboard can reconnect them automatically.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths
from .devices import DeviceError, run_adb

log = logging.getLogger(__name__)

DEFAULT_PORT = 5555
CONNECT_TIMEOUT_SECONDS = 15
PAIR_TIMEOUT_SECONDS = 30
RECONNECT_INTERVAL_SECONDS = 30
RECONNECT_TIMEOUT_SECONDS = 6

_ADDRESS_RE = re.compile(r"^(?P<host>[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\])(?::(?P<port>\d{1,5}))?$")
_IP_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")


def normalize_address(text: str, default_port: int | None = DEFAULT_PORT) -> str:
    """'192.168.1.20' → '192.168.1.20:5555'. Raises DeviceError for anything that isn't host[:port]."""
    text = (text or "").strip()
    match = _ADDRESS_RE.match(text)
    if not match:
        raise DeviceError(f"“{text}” is not an address. Use the phone's IP, e.g. 192.168.1.20:5555")
    port = match.group("port")
    if port is None:
        if default_port is None:
            raise DeviceError(f"Add the port to “{text}” (shown on the phone, e.g. {text}:37123)")
        port = str(default_port)
    if not 0 < int(port) < 65536:
        raise DeviceError(f"Port {port} is out of range")
    return f"{match.group('host')}:{port}"


def is_wireless(serial: str) -> bool:
    """True for Wi-Fi serials: 'ip:port' or the mDNS names adb gives Wireless debugging phones."""
    return ":" in serial or "._adb-tls-connect." in serial


def connect(address: str, timeout: float = CONNECT_TIMEOUT_SECONDS) -> str:
    """``adb connect``. Returns the serial to use. Raises DeviceError with a readable reason."""
    address = normalize_address(address)
    output = run_adb("connect", address, timeout=timeout).strip()
    # adb exits 0 even when the connection fails; only the text tells.
    lowered = output.lower()
    if "connected to" in lowered and "cannot" not in lowered and "failed" not in lowered:
        log.info("Wireless device connected: %s", address)
        return address
    reason = output or "no answer"
    if "refused" in lowered:
        reason += ". Is Wireless debugging on, or was the phone switched to Wi-Fi with USB first?"
    elif "timed out" in lowered or "no route" in lowered or "unreachable" in lowered:
        reason += ". Check that the phone and this PC are on the same Wi-Fi network."
    raise DeviceError(f"Could not connect to {address}: {reason}")


def disconnect(address: str) -> None:
    try:
        run_adb("disconnect", address)
        log.info("Wireless device disconnected: %s", address)
    except DeviceError as exc:  # already gone is fine
        log.debug("disconnect %s: %s", address, exc)


def pair(address: str, code: str, timeout: float = PAIR_TIMEOUT_SECONDS) -> None:
    """``adb pair`` with the 6-digit code from the phone's “Pair device with pairing code” screen."""
    address = normalize_address(address, default_port=None)
    code = re.sub(r"\s+", "", code or "")
    if not code.isdigit():
        raise DeviceError("Enter the pairing code shown on the phone (digits only)")
    output = run_adb("pair", address, code, timeout=timeout).strip()
    if "successfully paired" not in output.lower():
        raise DeviceError(f"Pairing with {address} failed: {output or 'no answer'}. "
                          "The code and port change each time the pairing screen opens.")
    log.info("Paired with %s", address)


def wifi_ip(serial: str) -> str:
    """The phone's Wi-Fi IPv4 address, read over its current (USB) connection."""
    for args in (("shell", "ip", "-f", "inet", "addr", "show", "wlan0"), ("shell", "ip", "-f", "inet", "addr")):
        try:
            output = run_adb(*args, serial=serial)
        except DeviceError:
            continue
        for ip in _IP_RE.findall(output):
            if not ip.startswith("127."):
                return ip
    raise DeviceError(f"{serial} has no Wi-Fi address. Turn on Wi-Fi on the phone and join the same "
                      "network as this PC.")


def switch_to_wifi(serial: str, port: int = DEFAULT_PORT, settle_seconds: float = 2.0) -> str:
    """Put a USB-connected phone into Wi-Fi mode and connect to it. Returns the new serial."""
    ip = wifi_ip(serial)
    run_adb("tcpip", str(port), serial=serial)
    address = f"{ip}:{port}"
    last_error: DeviceError | None = None
    for attempt in range(4):  # adbd restarts on the phone; give it a moment
        time.sleep(settle_seconds if attempt == 0 else settle_seconds / 2)
        try:
            return connect(address)
        except DeviceError as exc:
            last_error = exc
    raise last_error or DeviceError(f"Could not connect to {address}")


# ---------------------------------------------------------------- saved phones

@dataclass
class SavedDevice:
    address: str
    name: str = ""


class WirelessStore:
    """Remembered Wi-Fi phones plus the auto-reconnect switch, in configs/wireless_devices.json."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.configs_dir() / "wireless_devices.json"
        self._lock = threading.Lock()
        self.devices: list[SavedDevice] = []
        self.auto_reconnect = True
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.auto_reconnect = bool(data.get("auto_reconnect", True))
        self.devices = [SavedDevice(str(d["address"]), str(d.get("name", "")))
                        for d in data.get("devices", []) if isinstance(d, dict) and d.get("address")]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"auto_reconnect": self.auto_reconnect, "devices": [asdict(d) for d in self.devices]}
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def remember(self, address: str, name: str = "") -> None:
        for device in self.devices:
            if device.address == address:
                device.name = name or device.name
                break
        else:
            self.devices.append(SavedDevice(address, name))
        self.save()

    def forget(self, address: str) -> None:
        self.devices = [d for d in self.devices if d.address != address]
        self.save()


class AutoReconnector:
    """Reconnects saved Wi-Fi phones that dropped off, at most every RECONNECT_INTERVAL_SECONDS.

    Called from the dashboard's background device scan, so a phone that left the
    network costs a short timeout there, never a frozen window.
    """

    def __init__(self, store: WirelessStore, interval: float = RECONNECT_INTERVAL_SECONDS):
        self.store = store
        self.interval = interval
        self._last_attempt: float | None = None

    def run(self, present: list[str]) -> list[str]:
        """Try the saved phones missing from ``present``; returns the ones that came back."""
        if not self.store.auto_reconnect:
            return []
        if self._last_attempt is not None and time.monotonic() - self._last_attempt < self.interval:
            return []
        self._last_attempt = time.monotonic()
        back = []
        for device in list(self.store.devices):
            if device.address in present:
                continue
            try:
                back.append(connect(device.address, timeout=RECONNECT_TIMEOUT_SECONDS))
            except DeviceError as exc:
                log.debug("Auto-reconnect %s: %s", device.address, exc)
        return back
