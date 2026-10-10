"""Recovering when the Appium server stops while the app is open."""

from __future__ import annotations

import sys
import time

from core import appium_server, paths
from core.devices import connection_lost, friendly_error
from tests.fakes import FakeSession
from tests.test_polish import FAKE_APPIUM, _free_port
from tests.test_ui import wait_for

# The error from a user's screenshot (Windows, server stopped).
REFUSED = ("HTTPConnectionPool(host='127.0.0.1', port=4723): Max retries exceeded with url: "
           "/session/b7de4493-d59f-4e95-8bd7-c1978b70c88e/screenshot (Caused by NewConnectionError("
           "'<urllib3.connection.HTTPConnection object>: Failed to establish a new connection: [WinError 10061] "
           "No connection could be made because the target machine actively refused it'))")


class MaxRetryError(Exception):
    pass


def test_connection_problems_are_recognised_and_explained():
    assert connection_lost(MaxRetryError(REFUSED)) and connection_lost(RuntimeError(REFUSED))
    assert connection_lost(RuntimeError("Message: invalid session id: the session was deleted"))
    assert not connection_lost(RuntimeError("Element id=x not found within 3s"))
    assert "Lost the connection to the Appium server" in friendly_error(RuntimeError(REFUSED))
    assert friendly_error(RuntimeError("Message: no such element")) == "no such element"


def test_watchdog_restarts_a_stopped_server(tmp_path, monkeypatch):
    script = tmp_path / "fake_appium.py"
    script.write_text(FAKE_APPIUM)
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(appium_server, "find_appium_command", lambda: [sys.executable, str(script), "0"])
    url = f"http://127.0.0.1:{_free_port()}"
    watchdog = appium_server.AppiumWatchdog(url)
    try:
        assert watchdog.restart(wait_seconds=10) and appium_server.is_server_running(url)
        first = watchdog.server.process
        first.kill()  # the server dies while the app is open
        first.wait()
        assert not appium_server.is_server_running(url)
        assert not watchdog.restart(wait_seconds=10)  # throttled: not again within RESTART_INTERVAL
        watchdog._last_attempt -= watchdog.RESTART_INTERVAL
        assert watchdog.restart(wait_seconds=10) and appium_server.is_server_running(url)
        assert watchdog.server.process is not first and watchdog.restarts == 2
    finally:
        watchdog.stop()
    monkeypatch.setattr(appium_server, "find_appium_command", lambda: None)
    assert not appium_server.AppiumWatchdog(url).restart()  # nothing to start: just report


def test_dashboard_drops_dead_sessions_and_restarts_appium(app, window_factory, monkeypatch):
    import ui.dashboard as dashboard

    session = FakeSession("A1")
    window = window_factory({"A1": session})
    window.manager.session_for("A1")
    restarted = []

    class Watchdog:
        def can_restart(self):
            return True

        def due(self):
            return True

        def restart(self):
            restarted.append(1)
            return True

    window.appium_watchdog = Watchdog()
    monkeypatch.setattr(dashboard, "is_server_running", lambda url: True)
    window._apply_appium_state(True)
    window._apply_appium_state(False)  # server stopped
    assert not window.manager._sessions  # dead sessions forgotten: the next use reconnects
    assert "restarting" in window.appium_label.text()
    deadline = time.monotonic() + 5
    while not restarted and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert restarted


def test_picker_reconnects_after_a_lost_session(app, window_factory, monkeypatch):
    import ui.inspector as inspector_module

    class Flaky(FakeSession):
        failures = 1

        def screenshot_png(self):
            if Flaky.failures:
                Flaky.failures -= 1
                raise MaxRetryError(REFUSED)
            return super().screenshot_png()

    sessions = []

    def factory(serial, url):
        sessions.append(Flaky(serial))
        return sessions[-1]

    window = window_factory({"A1": FakeSession("A1")})
    window.manager._session_factory = factory
    monkeypatch.setattr(inspector_module, "is_server_running", lambda url: True)
    picker = inspector_module.InspectorWindow(window, on_pick=lambda capture: None, serial="A1")
    assert wait_for(app, lambda: bool(picker.elements))  # recovered by itself
    assert len(sessions) == 2  # the dead session was replaced by a fresh one
    picker.close()

    Flaky.failures = 2  # still failing after reconnecting, and the server is down
    monkeypatch.setattr(inspector_module, "is_server_running", lambda url: False)
    window.manager.drop_all_sessions()
    picker = inspector_module.InspectorWindow(window, on_pick=lambda capture: None, serial="A1")
    assert wait_for(app, lambda: "isn't running" in picker.status.text())
    picker.close()
