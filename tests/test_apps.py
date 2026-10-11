"""App names/icons read from the phone's APKs over adb, and the tap-to-pick helpers."""

import io
import re
import subprocess
import zipfile

import pytest

from core import apps
from tests.fakes import tiny_png


def _fake_dd(files):
    """subprocess.run stand-in for ``adb -s S exec-out "dd if=PATH bs=B skip=K count=N"``."""
    calls = []

    def run(command, **kwargs):
        script = command[-1]
        match = re.match(r"dd if=(\S+) bs=(\d+) skip=(\d+) count=(\d+)", script)
        path = match.group(1).strip("'")
        bs, skip, count = (int(match.group(i)) for i in (2, 3, 4))
        calls.append((path, skip, count))
        data = files[path][skip * bs:(skip + count) * bs]
        return subprocess.CompletedProcess(command, 0, data, b"")

    return run, calls


def test_remote_file_reads_only_what_zipfile_needs(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("big.bin", bytes(range(256)) * 4096)        # 1 MB we never read
        archive.writestr("res/icon.png", tiny_png(8, 8))
    data = buffer.getvalue()
    run, calls = _fake_dd({"/data/app/x/base.apk": data})
    monkeypatch.setattr(apps.subprocess, "run", run)

    remote = apps.RemoteFile("S1", "/data/app/x/base.apk", len(data))
    archive = zipfile.ZipFile(remote)
    assert archive.read("res/icon.png") == tiny_png(8, 8)
    fetched = sum(count for _, _, count in calls) * apps.BLOCK
    assert fetched < len(data) / 2  # the 1 MB entry was skipped
    remote.seek(0)
    assert remote.read(4) == b"PK\x03\x04"


def test_list_apps_and_stats_parse_adb_output(monkeypatch):
    outputs = {
        "pm list packages -f -3": "package:/data/app/~~a==/com.x-1==/base.apk=com.x\n"
                                  "package:/data/app/b/base.apk=com.b.app\nnoise\n",
        "stat": "123 1700000000 /data/app/b/base.apk\n",
    }

    def run_adb(*args, serial=None, timeout=None):
        joined = " ".join(args[1:]) if args[0] == "shell" else " ".join(args)
        return outputs["stat"] if joined.startswith("stat") else outputs[joined]

    monkeypatch.setattr(apps, "run_adb", run_adb)
    assert apps.list_apps("S1") == [("com.b.app", "/data/app/b/base.apk"),
                                    ("com.x", "/data/app/~~a==/com.x-1==/base.apk")]
    assert apps.apk_stats("S1", ["/data/app/b/base.apk"]) == {"/data/app/b/base.apk": (123, 1700000000)}


def test_app_info_is_cached_per_apk_version(monkeypatch, tmp_path):
    reads = []

    def fake_read(apk_file, other_apks=None):
        reads.append(apk_file.path)
        return "Shop", tiny_png(96, 96)

    monkeypatch.setattr(apps, "read_label_and_icon", fake_read)
    cache = apps.AppInfoCache(tmp_path)
    info = apps.app_info("S1", "com.shop", "/a/base.apk", (10, 1), cache)
    assert info.label == "Shop" and info.icon
    again = apps.app_info("S1", "com.shop", "/a/base.apk", (10, 1), cache)
    assert again.label == "Shop" and again.icon == info.icon and len(reads) == 1
    apps.app_info("S1", "com.shop", "/a/base.apk", (11, 2), cache)  # updated app: read again
    assert len(reads) == 2

    def broken(apk_file, other_apks=None):
        raise ValueError("not an apk")

    monkeypatch.setattr(apps, "read_label_and_icon", broken)
    assert apps.app_info("S1", "com.bad", "/b/base.apk", (1, 1), cache) == apps.AppInfo("com.bad")


@pytest.mark.parametrize("text, package", [
    ("  topResumedActivity=ActivityRecord{8f1 u0 com.whatsapp/.HomeActivity t12}", "com.whatsapp"),
    ("    mResumedActivity: ActivityRecord{a1 u0 com.android.chrome/com.google.Main t3}", "com.android.chrome"),
    ("  mCurrentFocus=Window{4f u0 com.facebook.katana/com.facebook.Main}", "com.facebook.katana"),
    ("nothing here", ""),
])
def test_parse_foreground(text, package):
    assert apps.parse_foreground(text) == package


def test_app_picker_and_tap_dialog(app, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QDialog

    from ui import app_picker

    monkeypatch.setattr(apps, "list_apps", lambda serial, third: [("com.b", "/b.apk"), ("com.a", "/a.apk")])
    monkeypatch.setattr(apps, "apk_stats", lambda serial, paths: {p: (1, 1) for p in paths})
    real_cache = apps.AppInfoCache
    monkeypatch.setattr(apps, "AppInfoCache", lambda: real_cache(tmp_path))
    names = {"com.a": ("Zeta", tiny_png(96, 96)), "com.b": ("Alpha", None)}
    monkeypatch.setattr(apps, "app_info",
                        lambda serial, package, apk, stat, cache: apps.AppInfo(package, *names[package]))

    dialog = app_picker.AppPickerDialog([("S1", "Phone")])
    from tests.test_ui import wait_for

    assert wait_for(app, lambda: dialog.message.text() == "2 apps")
    texts = [dialog.list.item(i).text() for i in range(dialog.list.count())]
    assert texts == ["Alpha", "Zeta"]  # sorted by real name once loaded
    dialog.filter.setText("zeta")
    assert dialog.selected_package() == "com.a"
    dialog.list_button.click()
    assert dialog.items["com.a"].text() == "Zeta   —   com.a"

    # Tap on the phone: the app that comes to the front is picked.
    taps = []
    monkeypatch.setattr(app_picker, "adb_screencap", lambda serial: tiny_png(40, 80))
    monkeypatch.setattr(apps, "home_package", lambda serial: "com.launcher")
    state = {"front": "com.launcher"}
    monkeypatch.setattr(apps, "foreground_package", lambda serial: state["front"])

    def tap(serial, x, y):
        taps.append((x, y))
        state["front"] = "com.picked"

    monkeypatch.setattr(apps, "tap", tap)
    monkeypatch.setattr(apps, "key", lambda serial, name: state.update(front="com.launcher"))
    tap_dialog = app_picker.PhoneTapDialog("S1", dialog)
    assert wait_for(app, lambda: tap_dialog.screen.source is not None and not tap_dialog._busy)
    tap_dialog._tap(10, 20)
    assert wait_for(app, lambda: tap_dialog.use_button.isEnabled(), timeout=8)
    assert taps == [(10, 20)] and tap_dialog.package == "com.picked"
    tap_dialog._accept()
    assert tap_dialog.result() == QDialog.Accepted
    dialog.select_package(tap_dialog.package)
    assert dialog.selected_package() == "com.picked"
