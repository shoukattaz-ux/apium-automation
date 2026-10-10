import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _close_qt_windows():
    """After each test, close windows and flush Qt's pending deletes (avoids a crash at exit)."""
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is not None:
        from ui.qtutil import close_all_windows

        close_all_windows(app)


# ------------------------------------------------------------------ shared UI fixtures
# (used by test_ui.py and test_appium_recovery.py; Qt is imported lazily so core tests run without it)

@pytest.fixture(scope="module")
def app():
    QApplication = pytest.importorskip("PySide6.QtWidgets").QApplication
    from ui.theme import apply_theme

    application = QApplication.instance() or QApplication([])
    apply_theme(application)
    return application


@pytest.fixture
def configs(tmp_path, monkeypatch):
    from core import paths

    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    (tmp_path / "configs" / "scripts").mkdir(parents=True)
    return tmp_path / "configs"


@pytest.fixture
def window_factory(app, configs, monkeypatch):
    import ui.dashboard as dashboard
    from core.manager import DeviceManager
    from core.scheduler import ScheduleStore

    monkeypatch.setattr(dashboard, "is_server_running", lambda url: True)
    created = []

    def make(sessions, models=None):
        manager = DeviceManager(session_factory=lambda serial, url: sessions[serial])
        window = dashboard.MainWindow(manager, scan_devices=False, schedules=ScheduleStore(configs / "schedules.json"))
        window.appium_timer.stop()
        window._apply_scan(list(sessions), models or {s: f"Phone {s}" for s in sessions}, "")
        created.append(window)
        return window

    yield make
    for window in created:
        window.manager.shutdown()
        for child in list(window._windows):
            child.close()
