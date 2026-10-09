"""Test doubles: a DeviceSession that acts on an in-memory 'screen' instead of a phone."""

from __future__ import annotations

import struct
import zlib

from core.devices import DeviceError, DeviceSession, ElementNotFound


def tiny_png(width: int = 4, height: int = 8) -> bytes:
    """A valid solid-gray PNG, for screenshot code paths."""
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PAGE_SOURCE = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy index="0" rotation="0" width="400" height="800">
  <android.widget.FrameLayout index="0" class="android.widget.FrameLayout" bounds="[0,0][400,800]">
    <android.widget.TextView index="0" class="android.widget.TextView" text="Order 1234"
        resource-id="com.shop:id/order" content-desc="" clickable="false" bounds="[20,40][380,100]"/>
    <android.widget.LinearLayout index="1" class="android.widget.LinearLayout" clickable="true"
        resource-id="com.shop:id/pay_row" bounds="[20,600][380,700]">
      <android.widget.TextView index="0" class="android.widget.TextView" text="Pay now"
          resource-id="" content-desc="" clickable="false" bounds="[40,620][200,680]"/>
    </android.widget.LinearLayout>
    <android.widget.TextView index="2" class="android.widget.TextView" text="Item"
        resource-id="com.shop:id/item" bounds="[20,200][380,260]"/>
    <android.widget.TextView index="3" class="android.widget.TextView" text="Item"
        resource-id="com.shop:id/item" bounds="[20,260][380,320]"/>
    <android.widget.ImageButton index="4" class="android.widget.ImageButton" content-desc="Menu"
        clickable="true" bounds="[340,0][400,40]"/>
  </android.widget.FrameLayout>
</hierarchy>"""


class FakeSession(DeviceSession):
    """Records actions; ``screen`` maps 'type=value' locators to element text."""

    def __init__(self, serial, screen=None, fail_times=None):
        super().__init__(serial, connect=False)
        self.driver = object()  # looks connected
        self.screen = dict(screen or {})
        self.calls = []
        self.fail_times = dict(fail_times or {})  # locator -> failures before success

    def _element(self, locator_type, locator_value):
        self._require_driver()
        key = f"{locator_type}={locator_value}"
        if self.fail_times.get(key, 0) > 0:
            self.fail_times[key] -= 1
            raise ElementNotFound(f"Element {key} not found (flaky)")
        if key not in self.screen:
            raise ElementNotFound(f"Element {key} not found")
        return key

    def open_app(self, package):
        self._require_driver()
        self.calls.append(("open_app", package))

    def close_app(self, package):
        self._require_driver()
        self.calls.append(("close_app", package))

    def click(self, locator_type, locator_value, timeout_seconds=15):
        self.calls.append(("click", self._element(locator_type, locator_value)))

    def wait_for_element(self, locator_type, locator_value, timeout_seconds=15):
        self._element(locator_type, locator_value)

    def exists(self, locator_type, locator_value, timeout_seconds=3):
        self._require_driver()
        return f"{locator_type}={locator_value}" in self.screen

    def copy_text(self, locator_type, locator_value, timeout_seconds=15):
        return self.screen[self._element(locator_type, locator_value)]

    def paste_text(self, locator_type, locator_value, text, timeout_seconds=15):
        key = self._element(locator_type, locator_value)
        self.screen[key] = text
        self.calls.append(("paste", key, text))

    def scroll(self, direction="down", times=1):
        self._require_driver()
        self.calls.append(("scroll", direction, times))

    def scroll_to_text(self, text):
        self._require_driver()
        if f"text={text}" not in self.screen:
            raise ElementNotFound(f"Could not scroll to text “{text}”")
        self.calls.append(("scroll_to_text", text))

    def window_size(self):
        return 400, 800

    def tap(self, x, y):
        self._require_driver()
        self.calls.append(("tap", int(x), int(y)))

    def swipe_percent(self, start_x, start_y, end_x, end_y, duration_ms=400):
        self._require_driver()
        self.calls.append(("swipe", start_x, start_y, end_x, end_y))

    def press_key(self, key):
        self._require_driver()
        self.calls.append(("key", key))

    def screenshot_png(self):
        self._require_driver()
        return tiny_png()

    def page_source(self):
        self._require_driver()
        return PAGE_SOURCE

    def current_package(self):
        return "com.fake"


class BrokenSession(FakeSession):
    """Every action reports a lost session."""

    def open_app(self, package):
        raise DeviceError("session is either terminated or not started")
