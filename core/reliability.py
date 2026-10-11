"""Keeping long runs on track: did the app react, surprise pop-ups, and lost phones.

* **Did the click work?** ``layout_signature`` and ``pixels_changed`` compare the screen
  before and after a click. Only when *both* the layout and the picture are unchanged
  is the tap considered lost (so a "like" that only changes an icon colour, or a feed
  that animates, is never tapped twice).
* **Pop-ups** ``find_popup`` recognises system dialogs that block a run (app not
  responding / keeps stopping, permission prompts, system notices, an open
  notification shade) and the button that safely closes each. Only used when a step's
  element can't be found, so a dialog the script itself wants to use is left alone.
* **Lost phone** ``wait_for_device`` brings a phone back for adb: reconnects Wi-Fi
  phones, revives an "offline" one and, if adb itself stopped answering, restarts the
  adb server (at most once a minute, since that resets every phone's connection).
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from lxml import etree

from .devices import DeviceError, parse_adb_devices, run_adb

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ did the app react?

_SIGNATURE_ATTRS = ("class", "resource-id", "text", "content-desc", "bounds", "checked", "selected",
                    "focused", "enabled", "package")


def layout_signature(page_source: str) -> str:
    """A fingerprint of everything on screen (elements, texts, states, positions)."""
    data = page_source.encode("utf-8") if isinstance(page_source, str) else page_source
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError:
        root = None
    digest = hashlib.sha1()
    if root is None:
        digest.update(data)
        return digest.hexdigest()
    for node in root.iter():
        if isinstance(node.tag, str) and node.get("bounds"):
            digest.update("|".join(node.get(a, "") for a in _SIGNATURE_ATTRS).encode("utf-8", "replace"))
            digest.update(b"\n")
    return digest.hexdigest()


STATUS_BAR_FRACTION = 0.05     # the clock and icons up there change on their own
COMPARE_SIZE = (180, 360)      # pictures are compared at this size (a small icon still spans dozens of pixels)
PIXEL_CHANGE_LEVEL = 10        # grey levels (0-255) a pixel must change by to count
PIXEL_CHANGE_COUNT = 6         # changed pixels that make "the screen changed" (screencap is lossless)


def pixels_changed(before_png: bytes, after_png: bytes) -> bool:
    """True if the screen picture changed visibly (ignoring the status bar)."""
    import numpy as np
    from PIL import Image

    def small(png: bytes):
        image = Image.open(io.BytesIO(png)).convert("L")
        top = int(image.height * STATUS_BAR_FRACTION)
        image = image.crop((0, top, image.width, image.height)).resize(COMPARE_SIZE)
        return np.asarray(image, dtype=np.int16)

    try:
        a, b = small(before_png), small(after_png)
    except Exception:  # unreadable picture: can't tell, so say "changed" (never tap twice on a guess)
        return True
    changed = np.abs(a - b) > PIXEL_CHANGE_LEVEL
    return int(changed.sum()) >= PIXEL_CHANGE_COUNT


# ------------------------------------------------------------------ pop-ups

SYSTEM_PACKAGES = frozenset({"android", "com.android.systemui"})
PERMISSION_PACKAGES = frozenset({
    "com.android.permissioncontroller", "com.google.android.permissioncontroller",
    "com.android.packageinstaller", "com.google.android.packageinstaller",
})
# Crash / not-responding dialogs: (resource id, what it does). Wait keeps a slow app alive.
APP_ERROR_BUTTONS = (
    ("android:id/aerr_wait", "Wait"),
    ("android:id/aerr_close", "Close app"),
    ("android:id/aerr_restart", "Open app again"),
)
PERMISSION_BUTTONS = {
    "allow": ("permission_allow_foreground_only_button", "permission_allow_button",
              "permission_allow_one_time_button"),
    "deny": ("permission_deny_button", "permission_deny_and_dont_ask_again_button"),
}
# Buttons that only close a notice from the system (never "Allow", "Delete", "Turn on"...).
SAFE_SYSTEM_BUTTONS = ("OK", "Got it", "Close", "Dismiss", "Not now", "Later", "No thanks", "Cancel", "Skip")
SHADE_IDS = ("com.android.systemui:id/notification_stack_scroller", "com.android.systemui:id/quick_settings_panel",
             "com.android.systemui:id/qs_frame", "com.android.systemui:id/notification_panel")


@dataclass(frozen=True)
class Popup:
    what: str                      # e.g. "“Shop isn't responding”"
    button: str                    # what is pressed, e.g. "Wait" or "Back"
    point: tuple[int, int] | None  # where to tap; None = press Back


def _bounds(node) -> tuple[int, int, int, int] | None:
    import re

    match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds", ""))
    return tuple(int(v) for v in match.groups()) if match else None


def _centre(node) -> tuple[int, int] | None:
    box = _bounds(node)
    if not box or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def _title(nodes) -> str:
    for node in nodes:
        rid = node.get("resource-id", "")
        if rid.endswith(("alertTitle", "aerr_title", "permission_message", "title")) and node.get("text"):
            return node.get("text")
    texts = [n.get("text") for n in nodes if n.get("text")]
    return texts[0] if texts else "a system dialog"


def find_popup(page_source: str, permissions: str = "leave", extra_buttons: tuple[str, ...] = ()) -> Popup | None:
    """The blocking pop-up on screen and how to close it, or None.

    ``permissions``: "leave" (don't touch permission prompts), "allow" or "deny".
    ``extra_buttons``: more button texts the user wants pressed when they block a step
    (e.g. an app's own "Not now" on a rating prompt), matched exactly, in any app.
    """
    data = page_source.encode("utf-8") if isinstance(page_source, str) else page_source
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError:
        return None
    if root is None:
        return None
    nodes = [n for n in root.iter() if isinstance(n.tag, str) and n.get("bounds")
             and n.get("displayed", "true") != "false"]

    by_id = {}
    for node in nodes:
        by_id.setdefault(node.get("resource-id", ""), node)
    for rid, label in APP_ERROR_BUTTONS:
        node = by_id.get(rid)
        if node is not None and _centre(node):
            return Popup(f"“{_title(nodes)}”", node.get("text") or label, _centre(node))

    permission_nodes = [n for n in nodes if n.get("package", "") in PERMISSION_PACKAGES]
    if permission_nodes and permissions in PERMISSION_BUTTONS:
        for wanted in PERMISSION_BUTTONS[permissions]:
            for node in permission_nodes:
                if node.get("resource-id", "").endswith(":id/" + wanted) and _centre(node):
                    return Popup(f"permission request “{_title(permission_nodes)}”",
                                 node.get("text") or permissions.capitalize(), _centre(node))

    if any(n.get("resource-id", "") in SHADE_IDS for n in nodes):
        return Popup("the notification panel", "Back", None)

    system = [n for n in nodes if n.get("package", "") in SYSTEM_PACKAGES]
    if system:
        for wanted in SAFE_SYSTEM_BUTTONS:
            for node in system:
                if (node.get("text", "").strip().lower() == wanted.lower()
                        and node.get("clickable") == "true" and _centre(node)):
                    return Popup(f"system notice “{_title(system)}”", node.get("text"), _centre(node))

    wanted = {b.strip().lower() for b in extra_buttons if b.strip()}
    if wanted:
        for node in nodes:
            text = (node.get("text") or node.get("content-desc") or "").strip().lower()
            if text in wanted and _centre(node):
                return Popup("a pop-up", node.get("text") or node.get("content-desc"), _centre(node))
    return None


# ------------------------------------------------------------------ getting a lost phone back

ADB_RESTART_COOLDOWN = 60.0
_adb_restart_lock = threading.Lock()
_last_adb_restart: float | None = None


def device_state(serial: str) -> str | None:
    """adb's view of the phone: "device", "offline", "unauthorized"... or None (not listed).

    Raises DeviceError if adb itself doesn't answer.
    """
    ready, problems = parse_adb_devices(run_adb("devices", timeout=15))
    if serial in ready:
        return "device"
    return problems.get(serial)


def restart_adb_server() -> bool:
    """Restart adb (it stopped answering). Rate-limited: it resets every phone's connection."""
    global _last_adb_restart
    with _adb_restart_lock:
        if _last_adb_restart is not None and time.monotonic() - _last_adb_restart < ADB_RESTART_COOLDOWN:
            return False
        _last_adb_restart = time.monotonic()
        log.warning("adb is not answering: restarting the adb server")
        for args in (("kill-server",), ("start-server",)):
            try:
                run_adb(*args, timeout=30)
            except DeviceError as exc:
                log.warning("adb %s: %s", args[0], exc)
        return True


def wait_for_device(serial: str, timeout: float, should_stop: Callable[[], bool] = lambda: False,
                    on_progress: Callable[[str], None] | None = None) -> bool:
    """Wait (and help) until adb sees the phone as ready again. False on timeout or stop."""
    deadline = time.monotonic() + timeout
    said = set()

    def say(message: str) -> None:
        if on_progress and message not in said:
            said.add(message)
            on_progress(message)

    while time.monotonic() < deadline and not should_stop():
        try:
            state = device_state(serial)
        except DeviceError as exc:
            say(f"adb is not answering ({exc}) — restarting adb")
            restart_adb_server()
            state = None
        if state == "device":
            return True
        try:
            if ":" in serial:  # Wi-Fi phone: ask adb to connect again
                say("reconnecting over Wi-Fi…")
                run_adb("connect", serial, timeout=15)
            elif state == "offline":
                say("phone is offline — asking adb to reconnect it…")
                run_adb("reconnect", "offline", timeout=15)
            elif state == "unauthorized":
                say("accept the “Allow USB debugging” prompt on the phone…")
            else:
                say("waiting for the phone to come back (check the cable)…")
        except DeviceError:
            pass
        end = time.monotonic() + 3
        while time.monotonic() < end and not should_stop():
            time.sleep(0.2)
    return False
