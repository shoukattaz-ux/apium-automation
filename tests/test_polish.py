"""Logging, crash handling, settings, paths, splash, menus/shortcuts and the Settings dialog."""

import logging
import sys
import threading
from pathlib import Path

import pytest

from core import logging_setup, paths
from core import settings as app_settings
from core.runner import ScriptRunner
from tests.fakes import FakeSession


@pytest.fixture(autouse=True)
def fresh_settings():
    app_settings.apply(app_settings.Settings())
    yield
    app_settings.apply(app_settings.Settings())


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    hooks = (sys.excepthook, threading.excepthook)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    logging_setup._file_handler = None
    sys.excepthook, threading.excepthook = hooks


# --------------------------------------------------------------------- logging


def test_rotating_dated_log_and_exception_hooks(tmp_path, restore_logging):
    log_file = logging_setup.setup_logging(tmp_path / "logs", console=False)
    assert log_file.name.startswith("app-") and log_file.suffix == ".log"
    assert logging_setup._file_handler.maxBytes == logging_setup.MAX_BYTES
    assert logging_setup._file_handler.backupCount == logging_setup.BACKUP_COUNT

    seen = []
    logging_setup.install_exception_hooks(lambda title, exc: seen.append(exc))
    try:
        raise ValueError("boom in a slot")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    worker = threading.Thread(target=lambda: 1 / 0, name="worker-x")
    worker.start()
    worker.join()

    logging_setup._file_handler.flush()
    text = log_file.read_text()
    assert "Unhandled exception" in text and "ValueError: boom in a slot" in text
    assert "Traceback" in text
    assert "Unhandled exception in thread worker-x" in text and "ZeroDivisionError" in text
    assert len(seen) == 1 and isinstance(seen[0], ValueError)

    # Moving the log folder (Settings) swaps the file without duplicating handlers.
    moved = logging_setup.setup_logging(tmp_path / "elsewhere", console=False)
    file_handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging_setup.RotatingFileHandler)]
    assert moved.parent == tmp_path / "elsewhere" and len(file_handlers) == 1


def test_emergency_crash_log(tmp_path):
    try:
        raise RuntimeError("before logging was ready")
    except RuntimeError as exc:
        path = logging_setup.write_emergency_log(exc, tmp_path)
    assert path.name.startswith("crash-") and "before logging was ready" in path.read_text()


def test_runner_and_manager_lines_reach_log_file_once(tmp_path, restore_logging):
    from core.manager import DeviceManager

    log_file = logging_setup.setup_logging(tmp_path, console=False)
    phones = {"s1": FakeSession("s1")}
    manager = DeviceManager(session_factory=lambda serial, url: phones[serial], history_dir=tmp_path / "runs")
    ui_lines = []
    manager.add_log_listener(lambda serial, message: ui_lines.append(message))
    assert manager.start("s1", {"name": "logged", "steps": [{"action": "open_app", "package": "p"},
                                                             {"action": "click", "locator_type": "id",
                                                              "locator_value": "missing", "timeout_seconds": 0}]})
    while manager.running_serials():
        threading.Event().wait(0.02)
    logging_setup._file_handler.flush()
    text = log_file.read_text()
    assert text.count("Running 'logged'") == 1  # not duplicated by manager + runner
    assert "WARNING" in text and "skipped" in text
    assert any("Running 'logged'" in line for line in ui_lines)  # UI panel still gets it
    manager.shutdown()


# --------------------------------------------------------------------- settings / paths


def test_settings_roundtrip_and_default_timeout(tmp_path):
    target = tmp_path / "settings.json"
    loaded = app_settings.load(target)
    assert loaded.default_timeout_seconds == 15
    app_settings.save(app_settings.Settings(default_timeout_seconds=42, log_dir=str(tmp_path / "L")), target)
    assert app_settings.load(target).default_timeout_seconds == 42
    assert paths.logs_dir() == tmp_path / "L"
    target.write_text("{not json")
    assert app_settings.load(target).default_timeout_seconds == 15  # corrupt file -> defaults

    app_settings.apply(app_settings.Settings(default_timeout_seconds=7))
    timeouts = []

    def find(step, session, main, timeout, quiet=False):
        timeouts.append(timeout)
        return session.candidates(*main)[0]

    runner = ScriptRunner(FakeSession("s1", {"id=x": ""}), {"name": "t", "steps": [
        {"action": "click", "locator_type": "id", "locator_value": "x"},
        {"action": "click", "locator_type": "id", "locator_value": "x", "timeout_seconds": 3},
    ]})
    runner._find = find
    runner.run()
    assert timeouts == [7.0, 3.0]


def test_data_dir_falls_back_when_install_folder_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path / "Program Files" / "App")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "_is_writable", lambda folder: False)
    assert paths.data_dir() == tmp_path / "local" / "DeviceAutomation"
    assert paths.configs_dir() == tmp_path / "local" / "DeviceAutomation" / "configs"
    monkeypatch.setattr(paths, "_is_writable", lambda folder: True)
    assert paths.data_dir() == tmp_path / "Program Files" / "App"


# --------------------------------------------------------------------- UI


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.theme import apply_theme

    application = QApplication.instance() or QApplication([])
    apply_theme(application)
    return application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    import ui.dashboard as dashboard
    from core.manager import DeviceManager
    from core.scheduler import ScheduleStore

    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(dashboard, "is_server_running", lambda url: True)
    win = dashboard.MainWindow(DeviceManager(session_factory=lambda s, u: FakeSession(s)), scan_devices=False,
                               schedules=ScheduleStore(tmp_path / "schedules.json"))
    win.appium_timer.stop()
    yield win
    win.manager.shutdown()


def test_theme_is_global(app):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QMessageBox

    from ui.theme import BG, STYLESHEET

    assert app.styleSheet() == STYLESHEET and "QMenu" in STYLESHEET and "QProgressBar" in STYLESHEET
    assert app.palette().color(QPalette.Window).name() == BG
    box = QMessageBox()  # any dialog created anywhere picks up the theme
    assert box.palette().color(QPalette.Window).name() == BG


def test_menus_and_shortcuts(window):
    from PySide6.QtGui import QKeySequence

    shortcuts = {key: action.shortcut().toString() for key, action in window.actions.items()}
    assert shortcuts["new"] == QKeySequence("Ctrl+N").toString()
    assert shortcuts["refresh"] == QKeySequence("F5").toString()
    assert shortcuts["settings"] == QKeySequence("Ctrl+,").toString()
    menu_titles = [a.text() for a in window.menuBar().actions()]
    assert menu_titles == ["&File", "&Devices", "&Tools", "&Help"]
    file_menu = window.menuBar().actions()[0].menu()
    assert window.actions["settings"] in file_menu.actions()
    assert window.actions["new"].iconText().endswith("New Script")  # toolbar label


def test_settings_dialog_saves_and_applies(window, tmp_path):
    from ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(window, window.manager)
    assert "1.1.0" in "".join(label.text() for label in dialog.findChildren(type(dialog.current_log)))
    dialog.timeout.setValue(25)
    dialog.appium_url.setText("http://127.0.0.1:4800")
    dialog.log_dir.setText(str(tmp_path / "mylogs"))
    original = logging_setup._file_handler
    try:
        dialog._save()
        assert app_settings.current().default_timeout_seconds == 25
        assert window.manager.appium_url == "http://127.0.0.1:4800"
        assert paths.logs_dir() == tmp_path / "mylogs"
        assert (paths.settings_file()).exists()
        assert logging_setup.current_log_file().parent == tmp_path / "mylogs"
    finally:
        if logging_setup._file_handler is not original:
            logging.getLogger().removeHandler(logging_setup._file_handler)
            logging_setup._file_handler.close()
            logging_setup._file_handler = original

    from ui.script_editor import StepDialog

    step = StepDialog(None)  # new steps start from the Settings timeout
    step.action_combo.setCurrentIndex(step.action_combo.findData("click"))
    assert step.inputs["timeout_seconds"].value() == 25


def test_splash_and_error_dialog(app, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox, QSplashScreen

    from ui.errors import show_error_dialog
    from ui.splash import SplashScreen

    splash = SplashScreen()
    splash.show()
    splash.step("Loading…", 40)
    assert splash.progress == 40 and not splash.pixmap().isNull()
    flag = {"done": False}
    threading.Timer(0.1, lambda: flag.update(done=True)).start()
    assert splash.wait_until(lambda: flag["done"], timeout=3, message="Scanning…", start=50, end=100)
    assert splash.progress == 100

    captured = {}

    def fake_exec(box):
        captured.update(text=box.text(), info=box.informativeText(), details=box.detailedText(),
                        splash_visible=any(isinstance(w, QSplashScreen) and w.isVisible()
                                           for w in app.topLevelWidgets()))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        raise KeyError("missing thing")
    except KeyError as exc:
        show_error_dialog(exc, fatal=False, log_file=Path(tmp_path / "logs" / "app-2026-01-01.log"))
    assert captured["text"].startswith("Something went wrong — details saved to")
    assert "app-2026-01-01.log" in captured["text"]
    assert "keep running" in captured["info"] and "KeyError" in captured["details"]
    assert captured["splash_visible"] is False  # the always-on-top splash never hides the dialog


# --------------------------------------------------------------------- Appium server start


FAKE_APPIUM = '''
import http.server, json, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
time.sleep(float(sys.argv[1]))  # pretend to be slow (antivirus scan on first start)
class Status(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"value": {"ready": True}}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", port), Status).serve_forever()
'''


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_slow_appium_start_waits_and_is_not_launched_twice(tmp_path, monkeypatch):
    from core import appium_server

    script = tmp_path / "fake_appium.py"
    script.write_text(FAKE_APPIUM)
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(appium_server, "find_appium_command", lambda: [sys.executable, str(script), "1.5"])
    server = appium_server.AppiumServer(f"http://127.0.0.1:{_free_port()}")
    ticks = []
    try:
        assert not server.start(wait_seconds=0.5, on_wait=ticks.append)  # still starting: times out
        first = server.process
        assert first is not None and first.poll() is None
        assert server.start(wait_seconds=10)  # "Retry": keeps waiting for the same process
        assert server.process is first and ticks
    finally:
        server.stop()


def test_stale_driver_manifest_is_reset(tmp_path):
    from core.appium_server import repair_driver_manifest

    cache = tmp_path / "home" / "node_modules" / ".cache" / "appium"
    cache.mkdir(parents=True)
    manifest = cache / "extensions.yaml"
    real = tmp_path / "home" / "node_modules" / "appium-uiautomator2-driver"
    real.mkdir()
    manifest.write_text(f"drivers:\n  uiautomator2:\n    installPath: {real}\n")
    assert not repair_driver_manifest(tmp_path / "home") and manifest.exists()  # paths fine: keep it
    manifest.write_text("drivers:\n  uiautomator2:\n    installPath: D:\\a\\build\\appium-home\\node_modules\\x\n")
    assert repair_driver_manifest(tmp_path / "home") and not manifest.exists()  # build machine path: reset
    assert not repair_driver_manifest(tmp_path / "missing")


def test_self_test_passes_from_source(capsys):
    import main

    assert main.self_test() == 0
    assert '"appium_client": "ok: Appium-Python-Client' in capsys.readouterr().out
