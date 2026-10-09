"""Regenerate assets/icon.png and a multi-size assets/icon.ico.

Each size is drawn separately (not just scaled down from 256 px) so the
16/24/32 px versions used by the title bar, taskbar and Explorer stay crisp.

    python packaging/make_icon.py        (needs PySide6 and Pillow)
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1d2027"))
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 52 * s, 52 * s)
    # Small sizes: thicker phone frames so they don't vanish.
    inset = 8 if size >= 48 else 12
    for x, color in ((52, "#3b82f6"), (132, "#22c55e")):
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(x * s, 52 * s, 72 * s, 128 * s), 14 * s, 14 * s)
        p.setBrush(QColor("#1d2027"))
        p.drawRoundedRect(QRectF((x + inset) * s, (52 + inset + 6) * s, (72 - 2 * inset) * s,
                                 (128 - 2 * inset - 12) * s), 6 * s, 6 * s)
    pen = QPen(QColor("#e6e8ec"))
    pen.setWidthF(max(1.5, (12 if size >= 48 else 18) * s))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QRectF(70 * s, 206 * s, 116 * s, 0).topLeft(), QRectF(186 * s, 206 * s, 0, 0).topLeft())
    p.drawLine(QRectF(166 * s, 190 * s, 0, 0).topLeft(), QRectF(186 * s, 206 * s, 0, 0).topLeft())
    p.drawLine(QRectF(166 * s, 222 * s, 0, 0).topLeft(), QRectF(186 * s, 206 * s, 0, 0).topLeft())
    p.end()
    return image


def to_pil(image: QImage) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)  # QPainter needs a GUI application
    images = {size: to_pil(draw(size)) for size in SIZES}
    ASSETS.mkdir(exist_ok=True)
    images[256].save(ASSETS / "icon.png")
    images[256].save(ASSETS / "icon.ico", format="ICO", sizes=[(s, s) for s in SIZES],
                     append_images=[images[s] for s in SIZES if s != 256])
    del app
    print(f"Wrote {ASSETS / 'icon.png'} and {ASSETS / 'icon.ico'} ({', '.join(map(str, SIZES))} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
