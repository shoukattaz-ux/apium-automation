"""Auto-update: release check, verified download, installer arguments, dialog (fake GitHub server)."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading

import pytest

from core import paths, updater
from core import settings as app_settings

INSTALLER = b"MZ fake installer " * 1000


@pytest.fixture
def github(monkeypatch):
    """A local server playing GitHub: /latest (API) and the two release assets."""
    state = {"tag": "v1.2.57", "installer": INSTALLER, "checksum": hashlib.sha256(INSTALLER).hexdigest(),
             "status": 200, "hits": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["hits"].append(self.path)
            base = f"http://127.0.0.1:{server.server_port}"
            if self.path == "/latest":
                if state["status"] != 200:
                    self.send_response(state["status"])
                    self.end_headers()
                    return
                body = json.dumps({"tag_name": state["tag"], "body": "## What's new\n- votes", "html_url": base,
                                   "assets": [
                                       {"name": "DeviceAutomation-Setup.exe", "size": len(state["installer"]),
                                        "browser_download_url": base + "/setup"},
                                       {"name": "DeviceAutomation-Setup.exe.sha256",
                                        "browser_download_url": base + "/sha"}]}).encode()
            elif self.path == "/setup":
                body = state["installer"]
            elif self.path == "/sha":
                body = f"{state['checksum']}  DeviceAutomation-Setup.exe\n".encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(updater, "API_URL", f"http://127.0.0.1:{server.server_port}/latest")
    monkeypatch.setattr(updater, "APP_VERSION", "1.2.5")
    yield state
    server.shutdown()


def test_version_comparison():
    assert updater.parse_version("v1.2.57") == (1, 2, 57)
    assert updater.is_newer("1.2.57", "1.2.5") and updater.is_newer("1.10.0", "1.9.99")
    assert not updater.is_newer("1.2.0", "1.2") and not updater.is_newer("1.2.4", "1.2.5")
    assert not updater.is_newer("garbage", "1.0.0")


def test_check_finds_a_newer_release_only(github):
    release = updater.check()
    assert release.version == "1.2.57" and release.installer_url.endswith("/setup") and "votes" in release.notes
    github["tag"] = "v1.2.5"
    assert updater.check() is None
    github["status"] = 404  # no release yet / repository made private
    assert updater.check() is None
    github["status"] = 500
    with pytest.raises(updater.UpdateError, match="500"):
        updater.check()


def test_release_without_installer_is_ignored():
    assert updater.parse_release({"tag_name": "v9.9.9", "assets": []}) is None
    assert updater.parse_release({"tag_name": "v9.9.9", "prerelease": True, "assets": [
        {"name": "DeviceAutomation-Setup.exe", "browser_download_url": "x"},
        {"name": "DeviceAutomation-Setup.exe.sha256", "browser_download_url": "y"}]}) is None


def test_download_is_verified(github, tmp_path):
    seen = []
    path = updater.download(updater.check(), tmp_path, progress=lambda done, total: seen.append((done, total)))
    assert path.read_bytes() == INSTALLER and seen[-1] == (len(INSTALLER), len(INSTALLER))
    github["installer"] = INSTALLER + b"tampered"
    with pytest.raises(updater.UpdateError, match="doesn't match"):
        updater.download(updater.check(), tmp_path / "again")
    assert not list((tmp_path / "again").glob("*.exe")) and not list((tmp_path / "again").glob("*.part"))
    github["checksum"] = "not-a-checksum"
    with pytest.raises(updater.UpdateError, match="checksum file"):
        updater.download(updater.check(), tmp_path / "third")


def test_installer_arguments(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path / "Programs" / "Device Automation Dashboard")
    args = updater.installer_arguments()
    assert {"/VERYSILENT", "/SUPPRESSMSGBOXES", "/RELAUNCH=1", "/CURRENTUSER"} <= set(args)
    assert f"/DIR={tmp_path / 'Programs' / 'Device Automation Dashboard'}" in args
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path / "Program Files" / "Device Automation Dashboard")
    assert "/ALLUSERS" in updater.installer_arguments()


def test_update_dialog_and_check_results(github, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui import update_dialog

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    app_settings.apply(app_settings.Settings())
    release = updater.check()

    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(("info", a[2])))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(("warning", a[2])))
    opened = []
    update_dialog.show_check_result(None, None, "", True, opened.append)
    update_dialog.show_check_result(None, None, "offline", True, opened.append)
    update_dialog.show_check_result(None, None, "offline", False, opened.append)  # silent at startup
    assert [kind for kind, _ in shown] == ["info", "warning"] and "latest version" in shown[0][1]

    dialog = update_dialog.UpdateDialog(None, release, busy=lambda: True)
    assert "1.2.57" in dialog.findChildren(type(dialog.status))[0].text()
    dialog.skip()
    assert app_settings.load().skipped_version == "1.2.57"
    update_dialog.show_check_result(None, release, "", False, opened.append)
    assert not opened  # skipped at startup...
    update_dialog.show_check_result(None, release, "", True, opened.append)
    assert opened == [release]  # ...but still offered when asked

    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    busy = update_dialog.UpdateDialog(None, release, busy=lambda: True)
    busy.install()
    assert "Stop them first" in busy.status.text()  # never updates under a running script

    started, quit_called = [], []
    monkeypatch.setattr(updater, "run_installer", started.append)
    monkeypatch.setattr(updater, "download", lambda r, progress=None: tmp_path / "setup.exe")
    ready = update_dialog.UpdateDialog(None, release, quit_app=lambda: quit_called.append(1))
    ready._downloaded(str(tmp_path / "setup.exe"), "")
    assert started == [str(tmp_path / "setup.exe")] and quit_called == [1]
