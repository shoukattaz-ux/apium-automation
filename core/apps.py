"""Apps installed on a phone: real names and icons, and "which app is open now".

Android has no adb command that returns an app's name or icon, so they are read from
the app's own APK on the phone. Only the few parts that are needed are fetched (the
zip directory, the manifest, the resource table and the icon file, about 1-5% of a
big APK) through ``adb exec-out dd`` byte ranges, so no APK is copied to the PC.
Results are cached on disk per APK version, so the second time is instant.

Also the helpers the "tap the app on the phone" picker uses: ``foreground_package``,
``home_package`` and adb taps/swipes/keys (no Appium session needed).
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import logging
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .devices import _NO_WINDOW, DeviceError, adb_path, run_adb

log = logging.getLogger(__name__)
logging.getLogger("pyaxmlparser").setLevel(logging.CRITICAL)  # very chatty about odd resource tables

BLOCK = 64 * 1024
ICON_SIZE = 96
READ_TIMEOUT_SECONDS = 60
CACHE_VERSION = 1


@dataclass
class AppInfo:
    package: str
    label: str = ""            # real app name ("" = not known yet / couldn't read)
    icon: bytes | None = None  # PNG, ICON_SIZE square


# ------------------------------------------------------------------ listing


def list_apps(serial: str, third_party_only: bool = True) -> list[tuple[str, str]]:
    """(package, base APK path) for installed apps, sorted by package."""
    args = ["shell", "pm", "list", "packages", "-f"] + (["-3"] if third_party_only else [])
    found = []
    for line in run_adb(*args, serial=serial).splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        apk, sep, package = line[len("package:"):].rpartition("=")
        if sep and package:
            found.append((package.strip(), apk.strip()))
    return sorted(found)


def apk_stats(serial: str, apks: list[str]) -> dict[str, tuple[int, int]]:
    """{apk path: (size, mtime)} in a few adb calls."""
    stats: dict[str, tuple[int, int]] = {}
    for start in range(0, len(apks), 25):
        chunk = apks[start:start + 25]
        command = "stat -c '%s %Y %n' " + " ".join(shlex.quote(p) for p in chunk) + " 2>/dev/null"
        try:
            output = run_adb("shell", command, serial=serial)
        except DeviceError:
            output = ""  # stat fails as a whole if one file vanished; the rest are read one by one
        for line in output.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                stats[parts[2]] = (int(parts[0]), int(parts[1]))
    return stats


def split_apks(serial: str, package: str) -> list[str]:
    """All APK files of a package (base + splits); density splits can hold the icon."""
    try:
        output = run_adb("shell", "pm", "path", package, serial=serial)
    except DeviceError:
        return []
    return [line.split(":", 1)[1].strip() for line in output.splitlines() if line.startswith("package:")]


# ------------------------------------------------------------------ reading APK parts over adb


class RemoteFile(io.RawIOBase):
    """A read-only, seekable view of a file on the phone, fetched in blocks with ``dd``."""

    def __init__(self, serial: str, path: str, size: int):
        super().__init__()
        self.serial, self.path, self.size = serial, path, size
        self.position = 0
        self.blocks: dict[int, bytes] = {}
        self.calls = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def _fetch(self, first: int, last: int) -> None:
        """Fetch blocks first..last (inclusive) in one adb call."""
        count = last - first + 1
        command = (f"dd if={shlex.quote(self.path)} bs={BLOCK} skip={first} count={count} 2>/dev/null")
        self.calls += 1
        try:
            result = subprocess.run([adb_path(), "-s", self.serial, "exec-out", command], capture_output=True,
                                    timeout=READ_TIMEOUT_SECONDS, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeviceError(f"Could not read {self.path}: {exc}") from exc
        data = result.stdout
        expected = min(count * BLOCK, self.size - first * BLOCK)
        if result.returncode != 0 or len(data) < expected:
            raise DeviceError(f"Could not read {self.path} (got {len(data)} of {expected} bytes)")
        for index in range(count):
            self.blocks[first + index] = data[index * BLOCK:(index + 1) * BLOCK]

    def read(self, size: int = -1) -> bytes:
        if self.position >= self.size:
            return b""
        end = self.size if size is None or size < 0 else min(self.size, self.position + size)
        first, last = self.position // BLOCK, (end - 1) // BLOCK
        missing = [b for b in range(first, last + 1) if b not in self.blocks]
        if missing:
            self._fetch(missing[0], missing[-1])
        data = b"".join(self.blocks[b] for b in range(first, last + 1))
        start = self.position - first * BLOCK
        chunk = data[start:start + (end - self.position)]
        self.position += len(chunk)
        return chunk

    def readinto(self, buffer) -> int:
        chunk = self.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)


# ------------------------------------------------------------------ name and icon from an APK


def read_label_and_icon(apk_file, other_apks=None) -> tuple[str, bytes | None]:
    """(app name, icon PNG or None) from an APK (path or file object).

    ``other_apks``: split APK files to look in when the icon picture isn't in the base.
    """
    from pyaxmlparser import APK

    apk = APK(apk_file)
    try:
        label = str(apk.application or "").strip()
    except Exception:
        label = ""
    icon = None
    try:
        icon = _icon_png(apk, other_apks or [])
    except Exception as exc:  # odd resource tables: name still useful without the icon
        log.debug("No icon for %s: %s", apk.package, exc)
    return label, icon


def _file_from(apk, other_apks, name: str) -> bytes | None:
    import zipfile

    for source in itertools.chain([apk.zip], other_apks):
        try:
            archive = source if isinstance(source, zipfile.ZipFile) else zipfile.ZipFile(source)
            return archive.read(name)
        except (KeyError, zipfile.BadZipFile, OSError, DeviceError):
            continue
    return None


def _icon_png(apk, other_apks) -> bytes | None:
    name = apk.get_app_icon()
    if name:
        data = _file_from(apk, other_apks, name)
        if data:
            return _to_png(data)
    return _adaptive_icon(apk, other_apks)


def _best_file(apk, reference: str) -> tuple[str | None, str | None]:
    """Resolve "@7f0d0001" to (best bitmap file, color) — whichever it is."""
    resources = apk.get_android_resources()
    if not resources or not reference.startswith("@"):
        return None, reference if reference.startswith("#") else None
    try:
        res_id = int(reference[1:].split(":")[-1], 16)
        candidates = resources.get_resolved_res_configs(res_id)
    except Exception:
        return None, None
    best, best_dpi, color = None, -1, None
    for config, value in candidates:
        value = str(value)
        if value.startswith("#"):
            color = color or value
        elif value.lower().endswith((".png", ".webp", ".jpg")):
            dpi = config.get_density()
            if dpi > best_dpi:
                best, best_dpi = value, dpi
    return best, color


def _adaptive_icon(apk, other_apks) -> bytes | None:
    """Adaptive icons (Android 8+) are XML layers: draw background + foreground bitmaps."""
    from pyaxmlparser.axmlprinter import AXMLPrinter

    reference = apk.get_attribute_value("application", "icon") or ""
    resources = apk.get_android_resources()
    if not reference.startswith("@") or not resources:
        return None
    res_id = int(reference[1:].split(":")[-1], 16)
    xml_name = next((str(v) for _, v in resources.get_resolved_res_configs(res_id)
                     if str(v).endswith(".xml")), None)
    data = _file_from(apk, other_apks, xml_name) if xml_name else None
    if not data:
        return None
    root = AXMLPrinter(data).get_xml_obj()
    android = "{http://schemas.android.com/apk/res/android}drawable"
    layers = {}
    for element in root:
        if element.tag in ("background", "foreground"):
            layers[element.tag] = element.get(android) or ""
    from PIL import Image

    size = ICON_SIZE * 3
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    drew = False
    for layer in ("background", "foreground"):
        file_name, color = _best_file(apk, layers.get(layer, ""))
        if file_name:
            picture = _file_from(apk, other_apks, file_name)
            if picture:
                image = Image.open(io.BytesIO(picture)).convert("RGBA").resize((size, size))
                canvas.alpha_composite(image)
                drew = drew or layer == "foreground"
        elif color and layer == "background":
            canvas = Image.new("RGBA", (size, size), _parse_color(color))
    if not drew:  # the foreground is a vector drawing: the caller shows a letter icon instead
        return None
    # An adaptive layer is 108dp with the middle 72dp visible: crop to what a launcher shows.
    margin = size // 6
    canvas = canvas.crop((margin, margin, size - margin, size - margin))
    return _to_png_image(_round_mask(canvas))


def _parse_color(text: str) -> tuple[int, int, int, int]:
    digits = text.lstrip("#")
    if len(digits) == 6:
        digits = "ff" + digits
    try:
        a, r, g, b = (int(digits[i:i + 2], 16) for i in range(0, 8, 2))
        return r, g, b, a
    except ValueError:
        return 128, 128, 128, 255


def _round_mask(image):
    from PIL import Image, ImageDraw

    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.size[0] - 1, image.size[1] - 1),
                                           radius=image.size[0] // 4, fill=255)
    image.putalpha(Image.composite(image.getchannel("A"), mask, mask))
    return image


def _to_png(data: bytes) -> bytes | None:
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    return _to_png_image(image)


def _to_png_image(image) -> bytes:
    from PIL import Image

    image = image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# ------------------------------------------------------------------ cache


def cache_dir() -> Path:
    return paths.data_dir() / "cache" / "app-icons"


def _cache_key(package: str, size: int, mtime: int) -> str:
    return hashlib.sha1(f"{CACHE_VERSION}|{package}|{size}|{mtime}".encode()).hexdigest()[:20]


class AppInfoCache:
    """Name + icon per APK version (package, size, time), as files in the data folder."""

    def __init__(self, folder: Path | None = None):
        self.folder = folder or cache_dir()
        self._lock = threading.Lock()

    def get(self, package: str, size: int, mtime: int) -> AppInfo | None:
        key = _cache_key(package, size, mtime)
        meta = self.folder / f"{key}.json"
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        icon_file = self.folder / f"{key}.png"
        icon = icon_file.read_bytes() if icon_file.is_file() else None
        return AppInfo(package, data.get("label", ""), icon)

    def put(self, info: AppInfo, size: int, mtime: int) -> None:
        key = _cache_key(info.package, size, mtime)
        try:
            with self._lock:
                self.folder.mkdir(parents=True, exist_ok=True)
                if info.icon:
                    (self.folder / f"{key}.png").write_bytes(info.icon)
                (self.folder / f"{key}.json").write_text(json.dumps({"package": info.package, "label": info.label}),
                                                         encoding="utf-8")
        except OSError as exc:
            log.debug("Could not cache app info: %s", exc)


def app_info(serial: str, package: str, apk: str, stat: tuple[int, int] | None,
             cache: AppInfoCache | None = None) -> AppInfo:
    """Name and icon of one app (from the cache when this APK version was read before)."""
    cache = cache or AppInfoCache()
    if stat is None:
        stat = apk_stats(serial, [apk]).get(apk)
        if stat is None:
            return AppInfo(package)
    cached = cache.get(package, *stat)
    if cached is not None:
        return cached
    try:
        base = RemoteFile(serial, apk, stat[0])
        label, icon = read_label_and_icon(base, _LazySplits(serial, package, apk))
    except Exception as exc:  # unreadable APK: the package name still works
        log.debug("Could not read %s: %s", package, exc)
        return AppInfo(package)
    info = AppInfo(package, label if label and not label.startswith("@") else "", icon)
    cache.put(info, *stat)
    return info


class _LazySplits:
    """Split APKs, only listed (one adb call) if the icon isn't in the base APK."""

    def __init__(self, serial: str, package: str, base: str):
        self.serial, self.package, self.base = serial, package, base
        self.files: list[RemoteFile] | None = None

    def __iter__(self):
        if self.files is None:
            self.files = []
            others = [p for p in split_apks(self.serial, self.package) if p != self.base]
            if others:
                stats = apk_stats(self.serial, others)
                self.files = [RemoteFile(self.serial, p, stats[p][0]) for p in others if p in stats]
        return iter(self.files)


# ------------------------------------------------------------------ tap-to-pick helpers (adb only)


_FOCUS = (
    re.compile(r"topResumedActivity=ActivityRecord\{\S+ \S+ ([\w.]+)/"),
    re.compile(r"mResumedActivity: ActivityRecord\{\S+ \S+ ([\w.]+)/"),
    re.compile(r"ResumedActivity: ActivityRecord\{\S+ \S+ ([\w.]+)/"),
    re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/"),
    re.compile(r"mFocusedApp=.*? ([\w.]+)/"),
)


def parse_foreground(dumpsys: str) -> str:
    for pattern in _FOCUS:
        match = pattern.search(dumpsys)
        if match:
            return match.group(1)
    return ""


def foreground_package(serial: str) -> str:
    """The app in front right now ("" if unknown)."""
    for command in ("dumpsys activity activities | grep -E 'ResumedActivity'",
                    "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"):
        try:
            found = parse_foreground(run_adb("shell", command, serial=serial))
        except DeviceError:
            continue
        if found:
            return found
    return ""


def home_package(serial: str) -> str:
    """The launcher (home screen) app."""
    try:
        output = run_adb("shell", "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                                  "-c android.intent.category.HOME", serial=serial)
    except DeviceError:
        return ""
    lines = [line.strip() for line in output.splitlines() if "/" in line]
    return lines[-1].split("/", 1)[0] if lines else ""


def tap(serial: str, x: int, y: int) -> None:
    run_adb("shell", "input", "tap", str(int(x)), str(int(y)), serial=serial)


def swipe(serial: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
    run_adb("shell", "input", "swipe", *(str(int(v)) for v in (x1, y1, x2, y2, ms)), serial=serial)


def key(serial: str, name: str) -> None:
    codes = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "recents": "KEYCODE_APP_SWITCH"}
    run_adb("shell", "input", "keyevent", codes.get(name, name), serial=serial)


def wait_for_foreground_change(serial: str, before: str, timeout: float = 4.0) -> str:
    """After a tap: the new app in front (or the old one if nothing opened)."""
    deadline = time.monotonic() + timeout
    current = before
    while time.monotonic() < deadline:
        time.sleep(0.4)
        current = foreground_package(serial) or current
        if current and current != before:
            time.sleep(0.6)  # let splash screens hand over to the real app
            return foreground_package(serial) or current
    return current
