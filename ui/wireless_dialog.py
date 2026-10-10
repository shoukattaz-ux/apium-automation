"""Wireless Devices dialog: connect phones over Wi-Fi instead of USB."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from core import wireless
from core.devices import DeviceError, device_model, list_connected_devices

from .qtutil import safe_emit

log = logging.getLogger(__name__)


class _Bridge(QObject):
    done = Signal(object, str, str)  # callback, result, error


class WirelessDialog(QDialog):
    """Switch a USB phone to Wi-Fi, pair (Android 11+), connect by IP, manage saved phones.

    ``on_change`` is called after any connect/disconnect so the dashboard rescans.
    adb work runs on a background thread; buttons are disabled meanwhile.
    """

    def __init__(self, parent=None, store: wireless.WirelessStore | None = None,
                 on_change: Callable[[], None] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Wireless Devices")
        self.setMinimumWidth(600)
        self.store = store or wireless.WirelessStore()
        self.on_change = on_change or (lambda: None)
        self.bridge = _Bridge(self)
        self.bridge.done.connect(self._finish)
        self.connected: list[str] = []
        self._busy_buttons: list[QPushButton] = []

        # ---- 1. switch a USB phone
        self.usb_combo = QComboBox()
        self.usb_combo.setMinimumWidth(260)
        self.switch_button = QPushButton("Switch to Wi-Fi")
        self.switch_button.setObjectName("Primary")
        self.switch_button.clicked.connect(self.switch_selected)
        usb_row = QHBoxLayout()
        usb_row.addWidget(self.usb_combo, 1)
        usb_row.addWidget(self.switch_button)
        usb_box = QGroupBox("1. Switch a USB phone to Wi-Fi (any Android)")
        usb_layout = QVBoxLayout(usb_box)
        usb_layout.addLayout(usb_row)
        usb_layout.addWidget(self._hint(
            "Phone and PC on the same Wi-Fi. Plug the phone in, click Switch, then unplug the cable. "
            "After the phone restarts, do this once more."))

        # ---- 2. pair (Android 11+)
        self.pair_address = QLineEdit()
        self.pair_address.setPlaceholderText("192.168.1.20:37123")
        self.pair_code = QLineEdit()
        self.pair_code.setPlaceholderText("123456")
        self.pair_code.setMaxLength(10)
        self.pair_button = QPushButton("Pair")
        self.pair_button.clicked.connect(self.pair_clicked)
        pair_box = QGroupBox("2. Pair without a cable (Android 11 and newer)")
        pair_layout = QVBoxLayout(pair_box)
        pair_form = QFormLayout()
        pair_layout.addWidget(self._hint(
            "On the phone: Settings → Developer options → Wireless debugging → "
            "“Pair device with pairing code”. Enter the IP address and port and the code it shows, "
            "then connect below with the address on the Wireless debugging screen (a different port)."))
        pair_layout.addLayout(pair_form)
        pair_form.addRow("Pairing IP and port", self.pair_address)
        code_row = QHBoxLayout()
        code_row.addWidget(self.pair_code, 1)
        code_row.addWidget(self.pair_button)
        pair_form.addRow("Pairing code", code_row)

        # ---- 3. connect by address
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.20:5555")
        self.address.returnPressed.connect(self.connect_clicked)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("Primary")
        self.connect_button.clicked.connect(self.connect_clicked)
        connect_box = QGroupBox("3. Connect by IP address")
        connect_row = QHBoxLayout(connect_box)
        connect_row.addWidget(self.address, 1)
        connect_row.addWidget(self.connect_button)

        # ---- saved phones
        self.saved_list = QListWidget()
        self.saved_list.setMinimumHeight(110)
        self.saved_list.currentRowChanged.connect(lambda _: self._update_saved_buttons())
        self.reconnect_button = QPushButton("Connect")
        self.reconnect_button.clicked.connect(self.reconnect_selected)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.disconnect_selected)
        self.forget_button = QPushButton("Forget")
        self.forget_button.setObjectName("Danger")
        self.forget_button.clicked.connect(self.forget_selected)
        self.auto_reconnect = QCheckBox("Reconnect saved phones automatically")
        self.auto_reconnect.setChecked(self.store.auto_reconnect)
        self.auto_reconnect.toggled.connect(self._auto_reconnect_toggled)
        saved_buttons = QHBoxLayout()
        saved_buttons.addWidget(self.auto_reconnect, 1)
        saved_buttons.addWidget(self.reconnect_button)
        saved_buttons.addWidget(self.disconnect_button)
        saved_buttons.addWidget(self.forget_button)
        saved_box = QGroupBox("Saved Wi-Fi phones")
        saved_layout = QVBoxLayout(saved_box)
        saved_layout.addWidget(self.saved_list)
        saved_layout.addLayout(saved_buttons)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        refresh = buttons.addButton("Refresh", QDialogButtonBox.ActionRole)
        refresh.clicked.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        for widget in (usb_box, pair_box, connect_box, saved_box, self.status, buttons):
            layout.addWidget(widget)

        self._busy_buttons = [self.switch_button, self.pair_button, self.connect_button, self.reconnect_button,
                              self.disconnect_button, self.forget_button, refresh]
        self.refresh()

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Muted")
        label.setWordWrap(True)
        return label

    # ------------------------------------------------------------------ background work

    def _run(self, busy_text: str, work: Callable[[], str], then: Callable[[str], None]) -> None:
        self._set_busy(True)
        self._show(busy_text)

        def target() -> None:
            try:
                safe_emit(self.bridge.done, then, work(), "")
            except DeviceError as exc:
                safe_emit(self.bridge.done, then, "", str(exc))
            except Exception as exc:  # report, never crash the dialog
                log.exception("Wireless action failed")
                safe_emit(self.bridge.done, then, "", f"Unexpected error: {exc}")

        threading.Thread(target=target, name="wireless", daemon=True).start()

    def _finish(self, then: Callable[[str], None], result: str, error: str) -> None:
        self._set_busy(False)
        if error:
            self._show(error, error=True)
        else:
            then(result)

    def _set_busy(self, busy: bool) -> None:
        for button in self._busy_buttons:
            button.setEnabled(not busy)
        if not busy:
            self.switch_button.setEnabled(self.usb_combo.count() > 0 and bool(self.usb_combo.currentData()))
            self._update_saved_buttons()

    def _show(self, text: str, error: bool = False) -> None:
        self.status.setObjectName("Error" if error else "Muted")
        self.status.setStyleSheet("color: #ff6b6b;" if error else "")
        self.status.setText(text)

    # ------------------------------------------------------------------ actions

    def refresh(self) -> None:
        def work() -> str:
            serials = list_connected_devices()
            usb = [s for s in serials if not wireless.is_wireless(s)]
            self._usb_models = {s: device_model(s) for s in usb}
            self.connected = serials
            return ""

        self._run("Looking for phones…", work, lambda _: self._apply_refresh())

    def _apply_refresh(self) -> None:
        self.usb_combo.clear()
        for serial, model in getattr(self, "_usb_models", {}).items():
            self.usb_combo.addItem(f"{model}  ({serial})", serial)
        if not self.usb_combo.count():
            self.usb_combo.addItem("No phone on USB — plug one in and click Refresh", "")
        self._fill_saved()
        self._set_busy(False)
        self._show("")

    def _fill_saved(self) -> None:
        current = self.selected_saved()
        self.saved_list.clear()
        for device in self.store.devices:
            online = device.address in self.connected
            label = f"{'●' if online else '○'}  {device.name or 'Phone'} — {device.address}"
            label += "   (connected)" if online else "   (not connected)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, device.address)
            self.saved_list.addItem(item)
            if device.address == current:
                self.saved_list.setCurrentItem(item)
        if not self.store.devices:
            item = QListWidgetItem("No saved phones yet")
            item.setFlags(Qt.NoItemFlags)
            self.saved_list.addItem(item)
        self._update_saved_buttons()

    def selected_saved(self) -> str:
        item = self.saved_list.currentItem()
        return item.data(Qt.UserRole) or "" if item else ""

    def _update_saved_buttons(self) -> None:
        address = self.selected_saved()
        online = address in self.connected
        self.reconnect_button.setEnabled(bool(address) and not online)
        self.disconnect_button.setEnabled(bool(address) and online)
        self.forget_button.setEnabled(bool(address))

    def _connected(self, address: str, name: str, message: str) -> None:
        self.store.remember(address, name)
        if address not in self.connected:
            self.connected.append(address)
        self._fill_saved()
        self._show(message)
        self.on_change()

    def switch_selected(self) -> None:
        serial = self.usb_combo.currentData()
        if not serial:
            return
        name = getattr(self, "_usb_models", {}).get(serial, "")

        self._run(f"Switching {name or serial} to Wi-Fi…", lambda: wireless.switch_to_wifi(serial),
                  lambda address: self._connected(
                      address, name, f"✔ {name or serial} is connected over Wi-Fi at {address}. "
                                     "You can unplug the cable now."))

    def pair_clicked(self) -> None:
        address, code = self.pair_address.text(), self.pair_code.text()

        def paired(_: str) -> None:
            host = address.strip().rsplit(":", 1)[0]
            self.address.setText(f"{host}:")
            self.address.setFocus()
            self.address.setCursorPosition(len(self.address.text()))
            self.pair_code.clear()
            self._show("✔ Paired. Now enter the port shown under “IP address & Port” on the Wireless "
                       "debugging screen and click Connect.")

        self._run("Pairing…", lambda: (wireless.pair(address, code), "")[1], paired)

    def connect_clicked(self) -> None:
        text = self.address.text()

        def work() -> str:
            address = wireless.connect(text)
            self._new_name = device_model(address)
            return address

        self._run("Connecting…", work, lambda address: self._connected(
            address, getattr(self, "_new_name", ""), f"✔ Connected to {address}."))

    def reconnect_selected(self) -> None:
        address = self.selected_saved()
        if address:
            self._run(f"Connecting to {address}…", lambda: wireless.connect(address),
                      lambda a: self._connected(a, "", f"✔ Connected to {a}."))

    def disconnect_selected(self) -> None:
        address = self.selected_saved()
        if not address:
            return

        def done(_: str) -> None:
            self.connected = [s for s in self.connected if s != address]
            self._fill_saved()
            self._show(f"Disconnected {address}.")
            self.on_change()

        # Otherwise auto-reconnect would undo a manual disconnect within seconds.
        if self.store.auto_reconnect:
            self.auto_reconnect.setChecked(False)
        self._run(f"Disconnecting {address}…", lambda: (wireless.disconnect(address), "")[1], done)

    def forget_selected(self) -> None:
        address = self.selected_saved()
        if address:
            self.store.forget(address)
            self._fill_saved()
            self._show(f"Forgot {address}.")

    def _auto_reconnect_toggled(self, checked: bool) -> None:
        self.store.auto_reconnect = checked
        self.store.save()
