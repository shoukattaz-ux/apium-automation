"""Wi-Fi connections, against a fake adb (no phone or network needed)."""

from __future__ import annotations

import pytest

from core import devices, paths, wireless
from core.devices import DeviceError


class FakeAdb:
    """Models one phone, serial USB123, IP 192.168.1.20, reachable over USB and (after tcpip) Wi-Fi."""

    def __init__(self):
        self.usb = True
        self.tcpip = False
        self.paired = False
        self.connected: list[str] = []
        self.calls: list[tuple] = []

    def __call__(self, *args, serial=None, timeout=10):
        self.calls.append((serial, *args))
        if args == ("devices",):
            lines = (["USB123\tdevice"] if self.usb else []) + [f"{a}\tdevice" for a in self.connected]
            return "List of devices attached\n" + "\n".join(lines) + "\n"
        if args[:2] == ("shell", "getprop"):
            return {"ro.serialno": "USB123\n", "ro.product.model": "V2344\n"}[args[2]]
        if args[:2] == ("shell", "ip"):
            return ("30: wlan0: <BROADCAST,UP> mtu 1500\n"
                    "    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0\n")
        if args[0] == "tcpip":
            self.tcpip = True
            return "restarting in TCP mode port: 5555\n"
        if args[0] == "connect":
            address = args[1]
            if address == "192.168.1.20:5555" and self.tcpip or address == "192.168.1.20:41000" and self.paired:
                if address not in self.connected:
                    self.connected.append(address)
                return f"connected to {address}\n"
            return f"failed to connect to '{address}': Connection refused\n"  # adb still exits 0
        if args[0] == "disconnect":
            self.connected.remove(args[1])
            return f"disconnected {args[1]}\n"
        if args[0] == "pair":
            if args[2] == "123456":
                self.paired = True
                return f"Successfully paired to {args[1]} [guid=adb-USB123]\n"
            return "Failed: Wrong password or connection was dropped.\n"
        raise AssertionError(f"unexpected adb call {args}")


@pytest.fixture
def adb(monkeypatch, tmp_path):
    fake = FakeAdb()
    monkeypatch.setattr(devices, "run_adb", fake)
    monkeypatch.setattr(wireless, "run_adb", fake)
    monkeypatch.setattr(devices, "_hardware_serials", {})
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    return fake


def test_normalize_address():
    assert wireless.normalize_address(" 192.168.1.20 ") == "192.168.1.20:5555"
    assert wireless.normalize_address("192.168.1.20:41000") == "192.168.1.20:41000"
    assert wireless.normalize_address("phone.local:5555") == "phone.local:5555"
    for bad in ("", "192.168.1.20:99999", "http://x", "a b"):
        with pytest.raises(DeviceError):
            wireless.normalize_address(bad)
    with pytest.raises(DeviceError, match="port"):
        wireless.normalize_address("192.168.1.20", default_port=None)
    assert wireless.is_wireless("192.168.1.20:5555")
    assert wireless.is_wireless("adb-USB123-abc._adb-tls-connect._tcp")
    assert not wireless.is_wireless("10FEAM01490006D")


def test_switch_usb_phone_to_wifi_and_dedupe(adb):
    assert devices.list_connected_devices() == ["USB123"]
    address = wireless.switch_to_wifi("USB123", settle_seconds=0)
    assert address == "192.168.1.20:5555"
    assert ("USB123", "tcpip", "5555") in adb.calls
    # Same phone on USB and Wi-Fi: listed once, by its Wi-Fi serial.
    assert devices.list_connected_devices() == ["192.168.1.20:5555"]
    adb.usb = False  # cable pulled
    assert devices.list_connected_devices() == ["192.168.1.20:5555"]


def test_connect_failure_is_reported_although_adb_exits_zero(adb):
    with pytest.raises(DeviceError, match="Wireless debugging"):
        wireless.connect("192.168.1.20")


def test_pair_then_connect(adb):
    with pytest.raises(DeviceError, match="Pairing .* failed"):
        wireless.pair("192.168.1.20:37000", "000000")
    with pytest.raises(DeviceError, match="digits"):
        wireless.pair("192.168.1.20:37000", "abc")
    wireless.pair("192.168.1.20:37000", "123 456")
    assert wireless.connect("192.168.1.20:41000") == "192.168.1.20:41000"


def test_store_and_auto_reconnect(adb, tmp_path):
    store = wireless.WirelessStore(tmp_path / "w.json")
    store.remember("192.168.1.20:5555", "V2344")
    store.remember("192.168.1.20:5555")  # no duplicate, name kept
    reloaded = wireless.WirelessStore(tmp_path / "w.json")
    assert [(d.address, d.name) for d in reloaded.devices] == [("192.168.1.20:5555", "V2344")]

    reconnector = wireless.AutoReconnector(reloaded, interval=3600)
    assert reconnector.run([]) == []          # phone not in Wi-Fi mode yet: quietly skipped
    adb.tcpip = True
    assert reconnector.run([]) == []          # throttled
    reconnector._last_attempt = None
    assert reconnector.run([]) == ["192.168.1.20:5555"]
    reloaded.auto_reconnect = False
    reconnector._last_attempt = None
    assert reconnector.run([]) == []
    reloaded.forget("192.168.1.20:5555")
    assert wireless.WirelessStore(tmp_path / "w.json").devices == []


def _wait_idle(app, dialog):
    import time

    deadline = time.time() + 5
    while not dialog.connect_button.isEnabled() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_wireless_dialog_switch_and_disconnect(adb, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.wireless_dialog import WirelessDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(wireless, "switch_to_wifi",
                        lambda serial: (adb("tcpip", "5555", serial=serial), wireless.connect("192.168.1.20"))[1])
    changes = []
    store = wireless.WirelessStore(tmp_path / "w.json")
    dialog = WirelessDialog(None, store, on_change=lambda: changes.append(1))
    _wait_idle(app, dialog)
    assert dialog.usb_combo.currentData() == "USB123" and "V2344" in dialog.usb_combo.currentText()

    dialog.switch_selected()
    _wait_idle(app, dialog)
    assert "unplug the cable" in dialog.status.text()
    assert [(d.address, d.name) for d in store.devices] == [("192.168.1.20:5555", "V2344")]
    assert changes and "(connected)" in dialog.saved_list.item(0).text()

    dialog.saved_list.setCurrentRow(0)
    assert dialog.disconnect_button.isEnabled() and not dialog.reconnect_button.isEnabled()
    dialog.disconnect_selected()
    _wait_idle(app, dialog)
    assert adb.connected == [] and not store.auto_reconnect
    assert "(not connected)" in dialog.saved_list.item(0).text()

    dialog.address.setText("10.0.0.1")
    dialog.connect_clicked()
    _wait_idle(app, dialog)
    assert "Could not connect" in dialog.status.text()
    dialog.close()
