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
   - Infinix/Tecno/itel: also turn on "Install via USB" and turn off
     "Verify apps over USB" (Developer options). The first connection installs
     Appium's helper app; allow any install prompt on the phone.

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
   - Open App / Close App steps: press "from phone..." to choose the app by
     its name and icon (no package ID needed). Or press "Tap on phone
     screen...", click the app's icon on the phone picture, then "Use this
     app". The first time, names and icons take a little while to load;
     after that they appear instantly.
   - Saved scripts are plain files in the "configs" folder next to the .exe.
     You can copy scripts between computers by copying that folder.
   - Script Library (toolbar "Library", or Ctrl+L): all your scripts and
     groups. Duplicate a script to make a changed copy, Rename it, or Delete
     it (it goes to the "configs\deleted" folder, so you can bring it back).

Groups (several scripts one after another)
   - Library > New Group (or Ctrl+G): give it a name, add scripts in the order
     they should run, and choose what happens when one fails:
       Retry: how many extra tries a failing script gets.
       Then: "Skip it and go on with the next script" (or stop the group).
       Stuck after: a script still running after this many minutes is
         stopped and counted as failed (0 = no limit).
       Ring the alarm when a script still fails.
   - The group appears in every phone's script list: pick it, press Start,
     and Script 1, 2, 3 ... run by themselves. Schedules and Run... can use
     groups too.

Reliability (File > Settings > Reliability; all on by default)
   - Click check: if a click changes nothing on screen, the app taps once more.
   - Pop-ups: "App isn't responding", "keeps stopping", system notices and an
     open notification panel are closed automatically when they block a step.
     Choose what to do with permission prompts, and add buttons of your own
     (e.g. "Not now, Skip") to press when they get in the way.
   - App guard: if a run ends up on the home screen or in another app, the
     script's app is brought back to the front automatically.
   - Reconnect: a phone that drops during a run is reconnected and the step
     is repeated.
   - Silent typing: text is filled in without the keyboard, in any language.

Alarm and Errors tab
   - When a script still fails after its retries, a ringtone plays. Press
     "Stop alarm" at the bottom of the window to silence it. Choose your own
     .wav sound and how many times it plays in File > Settings > Alerts.
   - The "Errors" tab lists every problem: which phone, which script and step,
     why, and a picture of the phone screen at that moment. Click a row to see
     it; "Open Report" shows the whole run.

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
   - Alerts: alarm on/off, the sound (.wav), how many times it plays, Test.
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
