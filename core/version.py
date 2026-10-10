"""Single source of truth for the app's name and version (used by the UI, build and installer).

Release builds (every push to main) overwrite APP_VERSION with "<major>.<minor>.<build number>",
so installed copies can tell which release is newer. Keep the major.minor part here.
"""

APP_NAME = "Device Automation Dashboard"
APP_ID = "DeviceAutomation"
APP_VERSION = "1.2.0"

# Where releases are published; the app checks it for updates (public repository: no token needed).
UPDATE_REPO = "shoukattaz-ux/apium-automation"
INSTALLER_ASSET = "DeviceAutomation-Setup.exe"
