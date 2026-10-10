"""Device Automation Dashboard — entry point.

    python main.py                       # open the dashboard
    python main.py --list-devices        # print connected phones and exit
    python main.py --run configs/x.json  # run a script on every phone, no UI
    python main.py --run x.json --devices SERIAL1,SERIAL2 --repeat 5 --delay 30
    python main.py --run workflow.json --roles A=SERIAL1,B=SERIAL2

Startup order: settings → logging (rotating file in the log folder) → global
exception hooks → splash screen → Appium check → dashboard → first device
scan. Any exception that escapes is written to the log with its traceback and
shown as a clean error dialog instead of the app vanishing.
"""

from __future__ import annotations

import argparse
import logging
import sys

from core import paths
from core import settings as app_settings
from core.devices import DEFAULT_APPIUM_URL
from core.logging_setup import install_exception_hooks, setup_logging, write_emergency_log
from core.version import APP_ID, APP_NAME, APP_VERSION

log = logging.getLogger("app")


# --------------------------------------------------------------------- CLI


def cli_list_devices(appium_url: str) -> int:
    """Phase 1 check: connect to every phone and show its foreground app."""
    from core.devices import connect_all_devices

    sessions = connect_all_devices(appium_url)
    if not sessions:
        print("No devices connected (check `adb devices` and that Appium is running).")
        return 1
    for serial, session in sessions.items():
        try:
            print(f"{serial}: foreground app = {session.current_package()}")
        finally:
            session.close()
    return 0


def cli_run(script_path: str, device_filter: str | None, roles: str | None, repeat: int,
            delay: float, appium_url: str) -> int:
    """Run a script without the UI (with run history), on all/chosen phones or as a workflow."""
    import time

    from core.devices import DeviceError, list_connected_devices
    from core.manager import DeviceManager
    from core.runner import RunState, load_any_script, needs_role_mapping, roles_of
    from core.schema import ScriptError

    try:
        script = load_any_script(script_path)
    except ScriptError as exc:
        print(exc)
        return 2
    manager = DeviceManager(appium_url)
    results = []
    manager.add_finished_listener(lambda serials, run_results: results.extend(run_results))
    try:
        if needs_role_mapping(script):
            if not roles:
                print(f"This script uses several phones; pass --roles "
                      f"{','.join(f'{r}=SERIAL' for r in roles_of(script))}")
                return 2
            mapping = dict(part.split("=", 1) for part in roles.split(",") if "=" in part)
            started = manager.start_workflow(mapping, script, repeat=repeat, delay_seconds=delay, trigger="cli")
        else:
            try:
                serials = ([s.strip() for s in device_filter.split(",") if s.strip()] if device_filter
                           else list_connected_devices())
            except DeviceError as exc:
                print(exc)
                return 1
            if not serials:
                print("No devices to run on.")
                return 1
            started = all([manager.start(s, script, repeat=repeat, delay_seconds=delay, trigger="cli")
                           for s in serials])
    except ScriptError as exc:
        print(exc)
        return 2
    if not started:
        print("Could not start on every phone.")
    try:
        while manager.running_serials():
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("Stopping…")
        manager.stop_all()
        while manager.running_serials():
            time.sleep(0.2)
    finally:
        manager.shutdown()
    return 1 if any(r.state == RunState.FAILED for r in results) else 0


def self_test() -> int:
    """Check that a (packaged) build can do what the app needs, without a phone or a window.

    Exercises the code paths that only run when a phone connects, so packaging
    mistakes (missing modules or package metadata) show up at build time instead
    of on the user's PC. Exit code 0 = all good. Results go to the log and to
    logs/self-test.txt (a windowed .exe has no console).
    """
    import importlib
    import json
    import traceback

    results: dict[str, str] = {}
    ok = True

    def check(name: str, func) -> None:
        nonlocal ok
        try:
            results[name] = "ok: " + str(func() or "")
        except Exception:  # report every failure, keep checking the rest
            ok = False
            results[name] = "FAILED: " + traceback.format_exc().strip().splitlines()[-1]

    def imports(*modules: str) -> None:
        for module in modules:
            importlib.import_module(module)

    def appium_client():
        imports("appium.webdriver", "appium.webdriver.common.appiumby")
        from appium.options.android import UiAutomator2Options
        from appium.version import version  # reads package metadata, which PyInstaller can drop
        options = UiAutomator2Options()
        options.udid = "self-test"
        return f"Appium-Python-Client {version}"

    def selenium_wait():
        imports("selenium.common.exceptions", "selenium.webdriver.support.expected_conditions",
                "selenium.webdriver.support.ui")
        import selenium
        return f"selenium {selenium.__version__}"

    def qt():
        imports("PySide6.QtWidgets")
        import PySide6
        return f"PySide6 {PySide6.__version__}"

    def element_matching():
        # Layout snapshots (lxml) and picture matching (numpy, Pillow): run them, not just import them.
        import io

        import numpy as np
        from PIL import Image, ImageDraw

        from core import imagematch
        from core.localfind import LocalScreen

        snapshot = LocalScreen('<hierarchy><node class="a" text="Go" bounds="[0,0][10,10]"/></hierarchy>')
        if len(snapshot.evaluate("xpath", '//*[@text="Go" and @class="a"]')) != 1:
            raise RuntimeError("layout snapshot found the wrong elements")
        screen = Image.new("L", (200, 300), 230)
        ImageDraw.Draw(screen).ellipse([60, 100, 120, 150], outline=0, width=5)
        buffer = io.BytesIO()
        screen.save(buffer, "PNG")
        shot = imagematch.load_gray(buffer.getvalue())
        match = imagematch.search(shot, shot[95:155, 55:125].copy())
        if not match or match.bounds[:2] != (55, 95):
            raise RuntimeError(f"picture matching found {match}")
        import lxml
        return f"numpy {np.__version__}, Pillow {Image.__version__}, lxml {lxml.__version__}"

    def bundled_tools():
        from core.appium_server import find_appium_command
        from core.devices import adb_path
        return f"appium={find_appium_command()} adb={adb_path()}"

    def example_scripts():
        from core.library import list_scripts
        from core.runner import load_any_script
        entries = list_scripts()
        for entry in entries:
            load_any_script(entry.path)
        return f"{len(entries)} scripts load"

    check("appium_client", appium_client)
    check("selenium", selenium_wait)
    check("qt", qt)
    check("element_matching", element_matching)
    check("bundled_tools", bundled_tools)
    check("example_scripts", example_scripts)
    report = json.dumps({"ok": ok, "version": APP_VERSION, "checks": results}, indent=2)
    (log.info if ok else log.error)("Self-test %s:\n%s", "passed" if ok else "FAILED", report)
    try:
        (paths.logs_dir() / "self-test.txt").write_text(report + "\n", encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:
        print(report)
    return 0 if ok else 1


# --------------------------------------------------------------------- GUI


def set_windows_app_id() -> None:
    """Give the process its own taskbar identity so Windows shows our icon, not Python's."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ID}.{APP_VERSION}")
        except (AttributeError, OSError):
            pass


def ensure_appium(app, appium_url: str, splash=None):
    """Make sure an Appium server is reachable, starting one if we can.

    Returns the AppiumServer we started (to stop on exit), or None.
    """
    from PySide6.QtWidgets import QMessageBox

    from core.appium_server import AppiumServer, find_appium_command, is_server_running

    if is_server_running(appium_url):
        log.info("Appium server is running at %s", appium_url)
        return None

    server = AppiumServer(appium_url)
    while True:
        if find_appium_command():
            if splash:
                splash.step("Starting the Appium server…", 40)
            log.info("Appium not running; starting it")

            def on_wait(elapsed: float) -> None:
                if splash:
                    note = " (the first start can take a minute or two)" if elapsed > 10 else ""
                    splash.step(f"Starting the Appium server… {elapsed:.0f}s{note}", 40 + min(15, int(elapsed / 10)))
                else:
                    app.processEvents()

            if server.start(on_wait=on_wait):
                log.info("Started Appium at %s", appium_url)
                return server
            if server.process is not None and server.process.poll() is None:
                detail = ("Appium is still starting (the first start can take a few minutes while "
                          "antivirus scans it). Wait a moment, then click Retry.")
            else:
                detail = "Appium was found but did not start. Details are in logs/appium-server.log."
        else:
            detail = "Appium isn't installed on this computer, and no bundled copy was found next to the app."
        log.warning("Appium server not available: %s", detail)

        if splash:
            splash.hide()  # a modal dialog must not be hidden behind the always-on-top splash
        box = QMessageBox(QMessageBox.Warning, APP_NAME,
                          "The Appium server isn't running, so phones can't be controlled yet.")
        box.setInformativeText(
            f"{detail}\n\nTo start it yourself: open a Command Prompt and run\n\n    appium\n\n"
            f"then click Retry. (Expected at {appium_url}.)")
        retry = box.addButton("Retry", QMessageBox.AcceptRole)
        box.addButton("Continue without Appium", QMessageBox.RejectRole)
        box.exec()
        if splash:
            splash.show()
        if box.clickedButton() is not retry:
            log.info("Continuing without Appium")
            # Keep a server we started: it may finish starting in the background.
            return server if server.process is not None else None
        if is_server_running(appium_url):
            return server if server.process is not None else None


def run_gui(appium_url: str) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from core.manager import DeviceManager
    from core.scheduler import ScheduleStore
    from ui.dashboard import MainWindow
    from ui.errors import show_error_dialog
    from ui.qtutil import close_all_windows
    from ui.splash import SplashScreen
    from ui.theme import apply_theme

    set_windows_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ID)
    apply_theme(app)  # one stylesheet + palette for every window and dialog
    if paths.icon_path().exists():
        app.setWindowIcon(QIcon(str(paths.icon_path())))
    # From here on, an exception in a Qt slot shows a dialog and the app keeps running.
    install_exception_hooks(lambda title, exc: show_error_dialog(exc, fatal=False))

    splash = SplashScreen()
    splash.show()
    splash.step("Loading scripts and schedules…", 15)
    schedules = ScheduleStore()
    splash.step("Checking the Appium server…", 35)
    server = ensure_appium(app, appium_url, splash)
    splash.step("Preparing the dashboard…", 60)
    from core.appium_server import AppiumWatchdog

    watchdog = AppiumWatchdog(appium_url, server)
    window = MainWindow(DeviceManager(appium_url), schedules=schedules, appium_watchdog=watchdog)
    splash.wait_until(lambda: window.scans_completed > 0, timeout=8, message="Scanning for phones…",
                      start=70, end=100)
    found = len(window.connected_devices())
    splash.step(f"Found {found} phone(s)" if found else "Ready", 100)
    window.show()
    splash.finish(window)
    if app_settings.current().check_updates:
        QTimer.singleShot(4000, window, lambda: window.check_for_updates(manual=False))
    log.info("%s %s ready (%d phone(s) connected)", APP_NAME, APP_VERSION, found)
    try:
        return app.exec()
    finally:
        close_all_windows(app)  # finish pending deletes before Python tears Qt down
        watchdog.stop()  # stops the server we started (at launch or after a restart)
        log.info("%s closed", APP_NAME)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--appium-url", default=None,
                        help=f"Appium server URL (default: Settings, then {DEFAULT_APPIUM_URL})")
    parser.add_argument("--list-devices", action="store_true", help="connect to all phones and exit")
    parser.add_argument("--run", metavar="SCRIPT", help="run a .json or .py script without the UI")
    parser.add_argument("--devices", help="comma-separated serials for --run (default: all)")
    parser.add_argument("--roles", help="phones for a cross-phone workflow, e.g. A=SERIAL1,B=SERIAL2")
    parser.add_argument("--repeat", type=int, default=1, help="runs per phone (0 = until Ctrl+C)")
    parser.add_argument("--delay", type=float, default=0, help="seconds between repeated runs")
    parser.add_argument("--self-test", action="store_true",
                        help="check the installation (no phone needed); exit code 0 = OK")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-level logging")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    args = parser.parse_args(argv)
    gui = not (args.list_devices or args.run or args.self_test)

    try:
        settings = app_settings.load()
        paths.ensure_user_dirs()
        log_file = setup_logging(paths.logs_dir(), verbose=args.verbose)
        install_exception_hooks()
        log.info("Starting %s %s (%s mode), log file %s", APP_NAME, APP_VERSION, "GUI" if gui else "CLI", log_file)
        appium_url = args.appium_url or settings.appium_url or DEFAULT_APPIUM_URL
        if args.self_test:
            return self_test()
        if args.list_devices:
            return cli_list_devices(appium_url)
        if args.run:
            return cli_run(args.run, args.devices, args.roles, args.repeat, args.delay, appium_url)
        return run_gui(appium_url)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # last line of defence: log it and tell the user
        return report_crash(exc, gui)


def report_crash(exc: BaseException, gui: bool) -> int:
    """Write the full traceback to the log (or a crash file) and show a clean dialog."""
    from core.logging_setup import current_log_file

    if current_log_file() is not None:
        log.critical("Fatal error — the app has to close", exc_info=exc)
        log_file = current_log_file()
    else:
        log_file = write_emergency_log(exc, paths.default_logs_dir())
    if gui:
        try:
            from ui.errors import ensure_app, show_error_dialog

            ensure_app()
            show_error_dialog(exc, fatal=True, log_file=log_file)
        except Exception:  # Qt itself may be what failed
            pass
    if sys.stderr is not None:
        print(f"Fatal error: {exc}. Details: {log_file}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
