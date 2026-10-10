DEVICE AUTOMATION DASHBOARD — QUICK START
==========================================

You do NOT need to install Python.
Installed with DeviceAutomation-Setup.exe? Start it from the Start Menu.
Got a folder instead? Keep the whole folder together and run DeviceAutomation.exe.

1. Install your phone's USB driver (Windows)
   - Samsung: "Samsung Android USB Driver for Windows"
   - Google Pixel: "Google USB Driver"
   - Others (Xiaomi, Oppo, ...): search "<brand> USB driver"
   Many phones work without this step; install it if the phone isn't found.

2. Turn on USB debugging on each phone
   - Settings > About phone > tap "Build number" 7 times (enables Developer options)
   - Settings > System > Developer options > turn on "USB debugging"
   - Xiaomi/Redmi: also turn on "USB debugging (Security settings)"

3. Plug in the phone(s) with a USB data cable
   - Unlock the phone and tap "Allow" on the "Allow USB debugging?" prompt
     (tick "Always allow from this computer").

4. Run DeviceAutomation.exe
   - The app starts Appium for you. If it says Appium isn't running, open a
     Command Prompt, run:  appium   and click Retry.
   - Phones appear in the Devices list within a few seconds. Plugged in a new
     one? Click "Add New Device".
   - Pick a script for each phone and press Start. Each phone runs on its own.

Wi-Fi instead of a cable (toolbar "Wi-Fi", or Devices > Wireless Devices, Ctrl+Shift+W)
   - The phone and this PC must be on the same Wi-Fi network.
   - Any Android: plug the phone in once, pick it under "Switch a USB phone to
     Wi-Fi", click "Switch to Wi-Fi", then unplug. Repeat after the phone restarts.
   - Android 11+, no cable: on the phone open Developer options > Wireless
     debugging > "Pair device with pairing code". Type the IP address & port
     and the code into "Pair", then "Connect" to the address shown on the
     Wireless debugging screen.
   - Connected phones are saved and reconnected automatically. Wi-Fi phones
     show "Wi-Fi" under their name in the Devices list.

Scripts
   - "New Script" opens the builder: add steps (open app, click, copy, paste,
     scroll, wait) with forms, then "Save Script".
   - Saved scripts are plain files in the "configs" folder next to the .exe.
     You can copy scripts between computers by copying that folder.

More tools
   - Element Picker: shows a live picture of the phone. Click a button to see
     how to find it, or tick "Record clicks" and just use the app — each click
     becomes a step.
   - Run...: run a script several times in a row, or a "workflow" that uses
     two phones (e.g. copy on phone A, paste on phone B).
   - Schedules: run a script every day at a set time, or every N minutes,
     while the app is open.
   - Run History tab: every run with its result. "Open Report" shows each
     step, with a screenshot of anything that failed (saved in "runs").

Settings (File > Settings, or Ctrl+,)
   - Default element timeout: how long steps wait for a button/text to appear.
   - Log folder and "Open Logs Folder": if something goes wrong, the app shows
     "Something went wrong — details saved to logs/..."; send that file.

Updates
   - The app checks for a new version when it starts (and Help > Check for
     Updates). Click "Install now": it downloads, checks and installs the update
     and reopens by itself. Your scripts and settings are kept.
   - Newest installer: github.com/shoukattaz-ux/apium-automation/releases/latest
   - Turn the automatic check off in File > Settings.

Troubleshooting
   - Phone not listed: re-plug the cable, check the "Allow USB debugging"
     prompt on the phone, try another cable/port (charge-only cables won't work).
   - A step is skipped: the button/text wasn't on screen in time. Edit the
     step and raise its timeout, or add a "Wait For Element" step before it.
   - Wi-Fi phone won't connect: check both are on the same network (not a
     guest network), keep the phone screen on, and switch it to Wi-Fi again
     with the cable if it was restarted. Some routers block phone-to-PC traffic
     ("AP/client isolation").
   - Logs are in the "logs" folder next to the .exe.
