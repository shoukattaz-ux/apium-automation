# Device Automation Dashboard

A Windows desktop app that drives 2–3 real Android phones at once through
Appium, to automate repetitive cross-app work (copy from one app, paste into
another, click through a third). Each phone gets its own Appium session and
thread, so starting, stopping or plugging in one phone never disturbs the
others.

![icon](assets/icon.png)

## Layout

| Path | What it is |
| --- | --- |
| `core/devices.py` | Phase 1: `list_connected_devices()`, `DeviceSession` (one UiAutomator2 session per phone), `connect_all_devices()`, and the shared action API (`open_app`, `click`, `scroll`, `wait`, `wait_for_element`, `copy_text`, `paste_text`, `tap`, `back`). |
| `core/schema.py` | The JSON script format, with validation shared by the engine and the builder. |
| `core/runner.py` | Phase 2: `ScriptRunner`, `StatusBoard`, `run_script_on_devices()`. Steps whose element isn't found are logged and skipped. |
| `core/manager.py` | Per-device run/session management for the dashboard. |
| `core/appium_server.py` | Detects Appium and starts a bundled or installed copy if needed. |
| `ui/dashboard.py` | Phase 3: device list, script picker, Start/Stop, live per-device log. |
| `ui/script_editor.py` | Phase 4 (form-based **Script Builder**) and Phase 5 (**Developer Mode** Python editor) in one window. |
| `configs/*.json` | Saved step scripts. `configs/scripts/*.py` holds developer scripts. Both are read at runtime, so new scripts need no rebuild. |
| `packaging/` | Phase 6: PyInstaller spec, Windows version info, build script, bundling script, end-user README. |

`core/` has no Qt imports, so it also runs from the command line and in tests.

## Running from source

Prerequisites: Python 3.10+, Android platform-tools (`adb`), and Appium 2+ with
the UiAutomator2 driver (`npm i -g appium && appium driver install uiautomator2`).

```bash
pip install -r requirements.txt
python main.py                                   # dashboard
python main.py --list-devices                    # Phase 1 check: each phone's foreground app
python main.py --run configs/example_copy_task.json            # run on every phone, no UI
python main.py --run my_task.json --devices SERIAL1,SERIAL2    # only some phones
python -m pytest -q                              # tests (fake devices, no phone needed)
```

The Appium URL defaults to `http://127.0.0.1:4723`; override it with
`--appium-url` or the `APPIUM_URL` environment variable.

## Script format

```json
{
  "name": "Copy task between apps",
  "steps": [
    {"action": "open_app", "package": "com.example.app"},
    {"action": "wait_for_element", "locator_type": "id", "locator_value": "com.example.app:id/title", "timeout_seconds": 15},
    {"action": "click", "locator_type": "id", "locator_value": "com.example.app:id/button"},
    {"action": "copy_text", "locator_type": "id", "locator_value": "com.example.app:id/text_field", "save_as": "copied_value"},
    {"action": "scroll", "direction": "down", "times": 2},
    {"action": "paste_text", "locator_type": "id", "locator_value": "com.other.app:id/input_field", "value_from": "copied_value"},
    {"action": "wait", "seconds": 3}
  ]
}
```

Locator types: `id`, `xpath`, `accessibility id`, `text` (exact visible text),
`class name`, `android uiautomator`. `click`, `copy_text` and `paste_text` also
accept an optional `timeout_seconds` (default 15). `paste_text` takes either
`value_from` (a variable saved by `copy_text`) or a fixed `text`.

Developer scripts are Python files defining `run(device)`; `device` is the same
`DeviceSession` the JSON engine uses. `print()` output goes to the dashboard log,
and an exception is reported as a traceback without affecting other phones.

## Building the Windows app

On a Windows machine, from the project root:

```bat
:: optional, once: bundle Node + Appium + UiAutomator2 + adb so users install nothing
powershell -ExecutionPolicy Bypass -File packaging\prepare_bundle.ps1

packaging\build.bat
```

This produces a one-folder build in `dist\DeviceAutomation\`; zip that folder to
share it. `configs\` sits next to `DeviceAutomation.exe`, so users can add or
edit scripts without rebuilding, and `README.txt` tells them how to set up their
phone. At startup the app checks for Appium: it starts the bundled (or
installed) copy if it can find one; otherwise it asks the user to run `appium`
and offers a Retry button.
