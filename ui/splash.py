"""Splash screen shown while the app starts (settings, scripts, Appium check, first device scan)."""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from core import paths
from core.version import APP_NAME, APP_VERSION

from .theme import ACCENT, BG, BORDER, SURFACE, TEXT, TEXT_MUTED

WIDTH, HEIGHT = 520, 300


class SplashScreen(QSplashScreen):
    """Logo, name, version, a status line and a progress bar, all painted to match the theme."""

    def __init__(self):
        super().__init__(QPixmap(WIDTH, HEIGHT), Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.message = "Starting…"
        self.progress = 0
        self.logo = QPixmap(str(paths.logo_path())) if paths.logo_path().exists() else QPixmap()
        self._render()

    def step(self, message: str, progress: int) -> None:
        """Update the status line and progress (0–100), and let Qt repaint."""
        self.message = message
        self.progress = max(0, min(100, progress))
        self._render()
        QApplication.processEvents()

    def wait_until(self, condition: Callable[[], bool], timeout: float, message: str, start: int, end: int) -> bool:
        """Keep the splash animating while waiting for ``condition`` (e.g. the first device scan)."""
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            if condition():
                self.step(message, end)
                return True
            fraction = (time.monotonic() - began) / timeout
            self.step(message, int(start + (end - start) * fraction))
            time.sleep(0.03)
        return False

    def _render(self) -> None:
        pixmap = QPixmap(WIDTH, HEIGHT)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        card = QPainterPath()
        card.addRoundedRect(QRectF(0.5, 0.5, WIDTH - 1, HEIGHT - 1), 16, 16)
        gradient = QLinearGradient(0, 0, 0, HEIGHT)
        gradient.setColorAt(0, QColor(SURFACE))
        gradient.setColorAt(1, QColor(BG))
        painter.fillPath(card, gradient)
        painter.setPen(QColor(BORDER))
        painter.drawPath(card)

        if not self.logo.isNull():
            painter.drawPixmap(QRectF(40, 52, 96, 96), self.logo, QRectF(self.logo.rect()))
        painter.setPen(QColor(TEXT))
        title = QFont(self.font())
        title.setBold(True)
        title_width = WIDTH - 156 - 32
        size = 19.0
        title.setPointSizeF(size)
        while size > 11 and QFontMetrics(title).horizontalAdvance(APP_NAME) > title_width:
            size -= 0.5  # shrink to fit rather than clip
            title.setPointSizeF(size)
        painter.setFont(title)
        painter.drawText(QRectF(156, 62, title_width, 36), Qt.AlignLeft | Qt.AlignVCenter, APP_NAME)
        small = QFont(self.font())
        small.setPointSizeF(10)
        painter.setFont(small)
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(156, 100, WIDTH - 180, 24), Qt.AlignLeft | Qt.AlignVCenter,
                         f"Version {APP_VERSION} · multi-phone Android automation")

        # status + progress bar
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(40, HEIGHT - 86, WIDTH - 80, 22), Qt.AlignLeft | Qt.AlignVCenter, self.message)
        track = QRectF(40, HEIGHT - 56, WIDTH - 80, 8)
        path = QPainterPath()
        path.addRoundedRect(track, 4, 4)
        painter.fillPath(path, QColor(BORDER))
        if self.progress:
            filled = QPainterPath()
            filled.addRoundedRect(QRectF(track.x(), track.y(), track.width() * self.progress / 100, track.height()), 4, 4)
            painter.fillPath(filled, QColor(ACCENT))
        painter.end()
        self.setPixmap(pixmap)
