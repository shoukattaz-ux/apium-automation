"""Pick an app from a phone: by name, by its icon, or by tapping it on the phone's screen.

* ``AppPickerDialog`` lists installed apps with their real names and icons (icon grid
  or list). The package list shows at once; names and icons fill in as they are read
  from the phone (cached, so the next time is instant).
* ``PhoneTapDialog`` shows the phone's screen: click an app icon on it, the app opens
  on the phone and the app in front is picked. Everything goes through adb, so no
  Appium session is needed.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListView,
    QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
)

from core import apps
from core.devices import DeviceError, adb_screencap

from .qtutil import safe_emit

WORKERS = 3
PACKAGE_ROLE = Qt.UserRole
LABEL_ROLE = Qt.UserRole + 1


def letter_icon(text: str, size: int = 96) -> QPixmap:
    """A colored rounded square with the first letter: for apps whose icon can't be drawn."""
    hue = int(hashlib.md5(text.encode()).hexdigest()[:2], 16) * 360 // 256
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor.fromHsv(hue, 150, 200))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, size, size, size // 4, size // 4)
    painter.setPen(Qt.white)
    font = QFont()
    font.setPixelSize(size // 2)
    font.setBold(True)
    painter.setFont(font)
    letter = next((c for c in text.split(".")[-1] if c.isalnum()), "?").upper()
    painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, letter)
    painter.end()
    return pixmap


class _Signals(QObject):
    listed = Signal(int, list, str)        # generation, [(package, apk)], error
    info = Signal(int, object)             # generation, AppInfo
    progress = Signal(int, int, int)       # generation, done, total


class AppPickerDialog(QDialog):
    """Installed apps with names and icons; returns the chosen package ID."""

    def __init__(self, devices: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick an app from a connected device")
        self.resize(640, 600)
        self.devices = devices
        self._signals = _Signals(self)
        self._signals.listed.connect(self._show_apps)
        self._signals.info.connect(self._apply_info)
        self._signals.progress.connect(self._show_progress)
        self._generation = 0
        self._progress = 0
        self._closed = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self.items: dict[str, QListWidgetItem] = {}

        self.device_combo = QComboBox()
        for serial, name in devices:
            self.device_combo.addItem(f"{name} ({serial})", serial)
        self.system_apps = QCheckBox("Include system apps")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or package, e.g. whatsapp")
        self.filter.setClearButtonEnabled(True)

        self.icons_button = QToolButton()
        self.icons_button.setText("▦  Icons")
        self.list_button = QToolButton()
        self.list_button.setText("☰  List")
        view_group = QButtonGroup(self)
        for button in (self.icons_button, self.list_button):
            button.setCheckable(True)
            view_group.addButton(button)
        self.icons_button.setChecked(True)
        view_group.buttonClicked.connect(lambda _: self._apply_view())
        self.tap_button = QPushButton("📱  Tap on phone screen…")
        self.tap_button.setToolTip("See the phone's screen here and click the app's icon on it")
        self.tap_button.clicked.connect(self.pick_on_phone)

        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        self.list.currentItemChanged.connect(lambda *_: self._update_choice())
        self.message = QLabel("")
        self.message.setObjectName("Muted")
        self.choice = QLabel("")
        self.choice.setTextInteractionFlags(Qt.TextSelectableByMouse)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setObjectName("Primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(self.device_combo, 1)
        top.addWidget(self.tap_button)
        options = QHBoxLayout()
        options.addWidget(self.filter, 1)
        options.addWidget(self.system_apps)
        options.addWidget(self.icons_button)
        options.addWidget(self.list_button)
        footer = QHBoxLayout()
        footer.addWidget(self.message, 1)
        footer.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(options)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.choice)
        layout.addLayout(footer)

        self.device_combo.currentIndexChanged.connect(lambda _: self.load())
        self.system_apps.toggled.connect(lambda _: self.load())
        self.filter.textChanged.connect(lambda _: self._apply_filter())
        self._apply_view()
        self._update_choice()
        if devices:
            self.load()
        else:
            self.message.setText("No phones connected.")
            self.tap_button.setEnabled(False)

    # ------------------------------------------------------------------ loading

    def serial(self) -> str | None:
        return self.device_combo.currentData()

    def load(self) -> None:
        serial = self.serial()
        if not serial:
            return
        self._generation += 1
        generation = self._generation
        self._progress = 0
        self.list.clear()
        self.items.clear()
        self.message.setText("Loading installed apps…")
        third_party_only = not self.system_apps.isChecked()

        def work() -> None:
            try:
                found = apps.list_apps(serial, third_party_only)
            except DeviceError as exc:
                safe_emit(self._signals.listed, generation, [], str(exc))
                return
            safe_emit(self._signals.listed, generation, found, "")
            self._read_details(serial, found, generation)

        threading.Thread(target=work, name="app-list", daemon=True).start()

    def _read_details(self, serial: str, found: list, generation: int) -> None:
        """Names and icons, a few apps at a time, newest list only."""
        stats = apps.apk_stats(serial, [apk for _, apk in found])
        cache = apps.AppInfoCache()
        done = 0
        lock = threading.Lock()

        def one(package: str, apk: str) -> None:
            nonlocal done
            if self._closed.is_set() or generation != self._generation:
                return
            info = apps.app_info(serial, package, apk, stats.get(apk), cache)
            with lock:
                done += 1
                count = done
            safe_emit(self._signals.info, generation, info)
            safe_emit(self._signals.progress, generation, count, len(found))

        executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="app-info")
        self._executor = executor
        for package, apk in found:
            executor.submit(one, package, apk)
        executor.shutdown(wait=True)

    def _show_apps(self, generation: int, found: list, error: str) -> None:
        if generation != self._generation:
            return
        if error:
            self.message.setText(error)
            return
        for package, _ in found:
            item = QListWidgetItem(QIcon(letter_icon(package)), package)
            item.setData(PACKAGE_ROLE, package)
            item.setData(LABEL_ROLE, "")
            item.setToolTip(package)
            self.items[package] = item
            self.list.addItem(item)
        self._apply_view()
        self._apply_filter()
        self.message.setText(f"{len(found)} apps · reading names and icons…" if found else "No apps found")

    def _apply_info(self, generation: int, info) -> None:
        item = self.items.get(info.package)
        if generation != self._generation or item is None:
            return
        if info.icon:
            pixmap = QPixmap()
            if pixmap.loadFromData(info.icon):
                item.setIcon(QIcon(pixmap))
        if info.label:
            item.setData(LABEL_ROLE, info.label)
            item.setToolTip(f"{info.label}\n{info.package}")
            item.setIcon(item.icon() if info.icon else QIcon(letter_icon(info.label)))
        self._set_text(item)
        self._apply_filter(keep=True)
        self._update_choice()

    def _show_progress(self, generation: int, done: int, total: int) -> None:
        # Worker threads can report out of order (2/2 before 1/2): only ever move forward.
        if generation != self._generation or done <= self._progress:
            return
        self._progress = done
        self.message.setText(f"{total} apps" if done >= total else
                             f"{total} apps · reading names and icons {done}/{total}…")
        if done >= total:
            self._sort_by_name()

    def _sort_by_name(self) -> None:
        current = self.selected_package()
        ordered = sorted(self.items.values(),
                         key=lambda i: ((i.data(LABEL_ROLE) or i.data(PACKAGE_ROLE)).lower(), i.data(PACKAGE_ROLE)))
        for item in ordered:
            self.list.takeItem(self.list.row(item))
        for item in ordered:
            self.list.addItem(item)
        if current in self.items:
            self.list.setCurrentItem(self.items[current])

    # ------------------------------------------------------------------ view

    def _icon_mode(self) -> bool:
        return self.icons_button.isChecked()

    def _set_text(self, item: QListWidgetItem) -> None:
        label, package = item.data(LABEL_ROLE), item.data(PACKAGE_ROLE)
        if not label:
            item.setText(package)
        elif self._icon_mode():
            item.setText(label)
        else:
            item.setText(f"{label}   —   {package}")

    def _apply_view(self) -> None:
        if self._icon_mode():
            self.list.setViewMode(QListView.IconMode)
            self.list.setIconSize(QSize(56, 56))
            self.list.setGridSize(QSize(112, 104))
            self.list.setResizeMode(QListView.Adjust)
            self.list.setMovement(QListView.Static)
            self.list.setTextElideMode(Qt.ElideRight)
        else:
            self.list.setViewMode(QListView.ListMode)
            self.list.setIconSize(QSize(28, 28))
            self.list.setGridSize(QSize())
        for item in self.items.values():
            self._set_text(item)

    def _apply_filter(self, keep: bool = False) -> None:
        needle = self.filter.text().strip().lower()
        first = None
        for item in self.items.values():
            text = f"{item.data(LABEL_ROLE)} {item.data(PACKAGE_ROLE)}".lower()
            hidden = bool(needle) and needle not in text
            item.setHidden(hidden)
            if not hidden and first is None:
                first = item
        current = self.list.currentItem()
        if not (keep and current is not None and not current.isHidden()):
            if current is None or current.isHidden():
                self.list.setCurrentItem(first)

    def _update_choice(self) -> None:
        item = self.list.currentItem()
        if item is None or item.isHidden():
            self.choice.setText("")
            self.ok_button.setEnabled(False)
            return
        label = item.data(LABEL_ROLE)
        package = item.data(PACKAGE_ROLE)
        self.choice.setText(f"Selected: <b>{label}</b> &nbsp; {package}" if label else f"Selected: {package}")
        self.ok_button.setEnabled(True)

    def selected_package(self) -> str | None:
        item = self.list.currentItem()
        return item.data(PACKAGE_ROLE) if item is not None and not item.isHidden() else None

    def select_package(self, package: str) -> None:
        """Select ``package`` in the list (added if it isn't there, e.g. a system app)."""
        item = self.items.get(package)
        if item is None:
            item = QListWidgetItem(QIcon(letter_icon(package)), package)
            item.setData(PACKAGE_ROLE, package)
            item.setData(LABEL_ROLE, "")
            self.items[package] = item
            self.list.insertItem(0, item)
        self.filter.clear()
        item.setHidden(False)
        self.list.setCurrentItem(item)
        self.list.scrollToItem(item)

    # ------------------------------------------------------------------ tap on the phone

    def pick_on_phone(self) -> None:
        serial = self.serial()
        if not serial:
            return
        dialog = PhoneTapDialog(serial, self)
        if dialog.exec() == QDialog.Accepted and dialog.package:
            self.select_package(dialog.package)
            self.accept()

    def done(self, result: int) -> None:  # noqa: A003 - Qt override
        self._closed.set()
        self._generation += 1  # drop results still on their way
        super().done(result)


# ======================================================================= tap on the phone screen


class _PhoneSignals(QObject):
    screen = Signal(bytes, str)          # png, error
    opened = Signal(str, str)            # package, error


class PhoneScreen(QLabel):
    """The phone's screenshot; a click is a tap, a drag is a swipe (in phone pixels)."""

    tapped = Signal(int, int)
    swiped = Signal(int, int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(260, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self.source: QPixmap | None = None
        self._press: QPoint | None = None

    def set_png(self, png: bytes) -> bool:
        pixmap = QPixmap()
        if not pixmap.loadFromData(png):
            return False
        self.source = pixmap
        self._rescale()
        return True

    def _rescale(self) -> None:
        if self.source is not None:
            self.setPixmap(self.source.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._rescale()

    def to_phone(self, point: QPoint) -> tuple[int, int] | None:
        shown = self.pixmap()
        if self.source is None or shown is None or shown.isNull():
            return None
        left = (self.width() - shown.width()) // 2
        top = (self.height() - shown.height()) // 2
        x, y = point.x() - left, point.y() - top
        if not (0 <= x < shown.width() and 0 <= y < shown.height()):
            return None
        scale = self.source.width() / shown.width()
        return int(x * scale), int(y * scale)

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        self._press = event.position().toPoint()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        start, self._press = self._press, None
        if start is None:
            return
        end = event.position().toPoint()
        a, b = self.to_phone(start), self.to_phone(end)
        if a is None:
            return
        if b is not None and (end - start).manhattanLength() > 20:
            self.swiped.emit(a[0], a[1], b[0], b[1])
        else:
            self.tapped.emit(a[0], a[1])


class PhoneTapDialog(QDialog):
    """Click an app's icon on the phone's screen; the app that opens is the one picked."""

    def __init__(self, serial: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Tap the app on the phone — {serial}")
        self.resize(460, 820)
        self.serial = serial
        self.package = ""
        self.home = ""
        self.before = ""
        self._before_tap = ""
        self._busy = False
        self._signals = _PhoneSignals(self)
        self._signals.screen.connect(self._show_screen)
        self._signals.opened.connect(self._opened)

        hint = QLabel("Click an app's icon on the picture: it opens on the phone and its package is picked. "
                      "Drag to swipe (e.g. to the next home page or up for the app drawer).")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        self.screen = PhoneScreen()
        self.screen.setText("Taking a screenshot…")
        self.screen.tapped.connect(self._tap)
        self.screen.swiped.connect(self._swipe)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        def nav(text: str, tip: str, slot) -> QPushButton:
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False: slot())
            return button

        self.nav_buttons = [
            nav("⌂  Home", "Phone's home screen", lambda: self._key("home")),
            nav("◁  Back", "Phone's back button", lambda: self._key("back")),
            nav("▲  App drawer", "Swipe up to show all apps", self._drawer),
            nav("⟳", "Take a new screenshot", self.refresh),
        ]
        navigation = QHBoxLayout()
        for button in self.nav_buttons:
            navigation.addWidget(button)

        self.go_home = QCheckBox("Go back to the home screen afterwards")
        self.go_home.setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.use_button = buttons.button(QDialogButtonBox.Ok)
        self.use_button.setText("Use this app")
        self.use_button.setObjectName("Primary")
        self.use_button.setEnabled(False)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(navigation)
        layout.addWidget(self.screen, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.go_home)
        layout.addWidget(buttons)

        def start() -> None:
            self.home = apps.home_package(serial)
            self.before = apps.foreground_package(serial)

        self._run(start, then_refresh=True)

    # ------------------------------------------------------------------ background work

    def _run(self, action, then_refresh: bool = True, opened: bool = False) -> None:
        """Run an adb action off the UI thread, then (optionally) a new screenshot."""
        self._set_busy(True)

        def work() -> None:
            try:
                result = action()
                if opened:
                    safe_emit(self._signals.opened, result or "", "")
            except DeviceError as exc:
                if opened:
                    safe_emit(self._signals.opened, "", str(exc))
                else:
                    safe_emit(self._signals.screen, b"", str(exc))
                return
            if then_refresh:
                self._capture()
            else:
                safe_emit(self._signals.screen, b"", "")

        threading.Thread(target=work, name="phone-tap", daemon=True).start()

    def _capture(self) -> None:
        try:
            safe_emit(self._signals.screen, adb_screencap(self.serial), "")
        except DeviceError as exc:
            safe_emit(self._signals.screen, b"", str(exc))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in self.nav_buttons:
            button.setEnabled(not busy)
        self.screen.setCursor(Qt.WaitCursor if busy else Qt.PointingHandCursor)

    def refresh(self) -> None:
        self._run(lambda: None)

    def _show_screen(self, png: bytes, error: str) -> None:
        self._set_busy(False)
        if error:
            self.status.setText(f"⚠ {error}")
        if png and not self.screen.set_png(png):
            self.status.setText("⚠ The phone sent an unreadable screenshot — press ⟳")

    # ------------------------------------------------------------------ actions

    def _tap(self, x: int, y: int) -> None:
        if self._busy:
            return
        self.status.setText("Opening…")
        before = self.before
        self._before_tap = before

        def action() -> str:
            apps.tap(self.serial, x, y)
            return apps.wait_for_foreground_change(self.serial, before)

        self._run(action, opened=True)

    def _opened(self, package: str, error: str) -> None:
        self.status.setTextFormat(Qt.RichText)
        if error:
            self.status.setText(f"⚠ {error}")
        elif not package or package == self.home or package == self._before_tap:
            self.status.setText("No app opened — click an app icon (drag to swipe to other pages).")
        elif package == "com.android.systemui":
            self.status.setText("That opened a system panel — press Back and click an app icon.")
        else:
            self.package = package
            self.before = package
            self.status.setText(f"Opened: <b>{package}</b> — press “Use this app”, or click another icon "
                                "(press Home first).")
            self.use_button.setEnabled(True)

    def _swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        if not self._busy:
            self._run(lambda: apps.swipe(self.serial, x1, y1, x2, y2))

    def _key(self, name: str) -> None:
        if self._busy:
            return

        def action() -> None:
            apps.key(self.serial, name)
            self.before = apps.foreground_package(self.serial)

        self._run(action)

    def _drawer(self) -> None:
        source = self.screen.source
        if self._busy or source is None:
            return
        w, h = source.width(), source.height()
        self._run(lambda: apps.swipe(self.serial, w // 2, int(h * 0.85), w // 2, int(h * 0.3), 350))

    def _accept(self) -> None:
        if not self.package:
            return
        if self.go_home.isChecked():
            serial = self.serial
            threading.Thread(target=lambda: _quietly(apps.key, serial, "home"), daemon=True).start()
        self.accept()


def _quietly(function, *args) -> None:
    try:
        function(*args)
    except DeviceError:
        pass


# The Script Builder's name for it.
PackagePickerDialog = AppPickerDialog

