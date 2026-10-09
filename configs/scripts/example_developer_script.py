"""Example Developer Mode script.

The app calls run(device) on the device you pick. `device` has the same
actions the form builder uses:

    device.open_app(package)
    device.click(locator_type, locator_value, timeout_seconds=15)
    device.wait_for_element(locator_type, locator_value, timeout_seconds=15)
    device.copy_text(locator_type, locator_value) -> str
    device.paste_text(locator_type, locator_value, text)
    device.scroll(direction="down", times=1)
    device.wait(seconds)
    device.tap(x, y), device.back(), device.current_package()

Locator types: "id", "xpath", "accessibility id", "text", "class name",
"android uiautomator". print() output appears in the dashboard log.
"""


def run(device):
    device.open_app("com.android.settings")
    device.wait_for_element("text", "About phone", timeout_seconds=15)
    device.click("text", "About phone")
    value = device.copy_text("id", "android:id/summary")
    print("Copied:", value)
    device.scroll("down", times=2)
    print("Foreground app is now", device.current_package())
