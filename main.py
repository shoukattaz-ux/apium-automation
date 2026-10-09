"""Device Automation Dashboard — entry point.

    python main.py                       # open the dashboard
    python main.py --list-devices        # print connected phones and exit
    python main.py --run configs/x.json  # run a script on every phone, no UI
    python main.py --run x.json --devices SERIAL1,SERIAL2
"""

from __future__ import annotations

import argparse
import logging
import sys

from core import paths
from core.devices import DEFAULT_APPIUM_URL

APP_NAME = "Device Automation Dashboard"
APP_VERSION = "1.0.0"


def configure_logging(verbose: bool = False) -> None:
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(paths.logs_dir() / "app.log", encoding="utf-8")]
    if sys.stderr is not None:  # windowed PyInstaller builds have no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


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


def cli_run(script_path: str, device_filter: str | None, appium_url: str) -> int:
    from core.devices import connect_all_devices
    from core.runner import RunState, run_script_on_devices

    sessions = connect_all_devices(appium_url)
    if device_filter:
        wanted = {s.strip() for s in device_filter.split(",") if s.strip()}
        sessions = {k: v for k, v in sessions.items() if k in wanted}
    if not sessions:
        print("No devices to run on.")
        return 1
    handle = run_script_on_devices(script_path, sessions, on_log=lambda s, m: print(f"[{s}] {m}"))
    try:
        while handle.is_running():
            handle.join(timeout=0.5)
    except KeyboardInterrupt:
        print("Stopping…")
        handle.stop()
        handle.join()
    finally:
        for session in sessions.values():
            session.close()
    failed = [s for s, r in handle.results.items() if r.state == RunState.FAILED]
    return 1 if failed else 0


# --------------------------------------------------------------------- GUI


def ensure_appium(app, appium_url: str):
    """Make sure an Appium server is reachable, starting one if we can.

    Returns the AppiumServer we started (to stop on exit), or None.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    from core.appium_server import AppiumServer, find_appium_command, is_server_running

    if is_server_running(appium_url):
        return None

    server = AppiumServer(appium_url)
    while True:
        if find_appium_command():
            progress = QProgressDialog("Starting the Appium server…", None, 0, 0)
            progress.setWindowTitle(APP_NAME)
            progress.setWindowModality(Qt.ApplicationModal)
            progress.setMinimumDuration(0)
            progress.show()
            app.processEvents()
            started = server.start()
            progress.close()
            if started:
                return server
            detail = ("Appium was found but did not start. Details are in logs/appium-server.log.")
        else:
            detail = ("Appium isn't installed on this computer, and no bundled copy was found next to the app.")

        box = QMessageBox(QMessageBox.Warning, APP_NAME,
                          "The Appium server isn't running, so phones can't be controlled yet.")
        box.setInformativeText(
            f"{detail}\n\nTo start it yourself: open a Command Prompt and run\n\n    appium\n\n"
            f"then click Retry. (Expected at {appium_url}.)")
        retry = box.addButton("Retry", QMessageBox.AcceptRole)
        box.addButton("Continue without Appium", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not retry:
            return None
        if is_server_running(appium_url):
            return None


def run_gui(appium_url: str) -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from core.manager import DeviceManager
    from ui.dashboard import MainWindow
    from ui.theme import STYLESHEET

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    if paths.icon_path().exists():
        app.setWindowIcon(QIcon(str(paths.icon_path())))

    server = ensure_appium(app, appium_url)
    window = MainWindow(DeviceManager(appium_url))
    window.show()
    try:
        return app.exec()
    finally:
        if server:
            server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--appium-url", default=DEFAULT_APPIUM_URL, help="Appium server URL")
    parser.add_argument("--list-devices", action="store_true", help="connect to all phones and exit")
    parser.add_argument("--run", metavar="SCRIPT", help="run a .json or .py script without the UI")
    parser.add_argument("--devices", help="comma-separated serials for --run (default: all)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    paths.ensure_user_dirs()
    configure_logging(args.verbose)
    if args.list_devices:
        return cli_list_devices(args.appium_url)
    if args.run:
        return cli_run(args.run, args.devices, args.appium_url)
    return run_gui(args.appium_url)


if __name__ == "__main__":
    sys.exit(main())
