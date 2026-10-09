DEVICE AUTOMATION DASHBOARD — QUICK START
==========================================

You do NOT need to install Python. Keep this whole folder together.

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

Scripts
   - "New Script" opens the builder: add steps (open app, click, copy, paste,
     scroll, wait) with forms, then "Save Script".
   - Saved scripts are plain files in the "configs" folder next to the .exe.
     You can copy scripts between computers by copying that folder.

Troubleshooting
   - Phone not listed: re-plug the cable, check the "Allow USB debugging"
     prompt on the phone, try another cable/port (charge-only cables won't work).
   - A step is skipped: the button/text wasn't on screen in time. Edit the
     step and raise its timeout, or add a "Wait For Element" step before it.
   - Logs are in the "logs" folder next to the .exe.
