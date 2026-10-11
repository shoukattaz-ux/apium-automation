"""Audible alarm when a script still fails after its retries.

Plays a WAV file (the user's own, or a built-in two-tone ringtone generated on first
use) a set number of times on a background thread, so a run is never held up by it.
``stop()`` silences it at once (the dashboard's "Stop alarm" button). Windows plays
through ``winsound`` (part of Python, no extra dependency); elsewhere a terminal bell
is the fallback. No Qt here, so it is unit tested with a fake player.
"""

from __future__ import annotations

import logging
import math
import struct
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable

from . import paths
from . import settings as app_settings

log = logging.getLogger(__name__)

BUILTIN_NAME = "alert-ringtone.wav"


def builtin_ringtone(folder: Path | None = None) -> Path:
    """A short two-tone ringtone (about 1.2 s), written once to the data folder."""
    target = (folder or paths.data_dir()) / BUILTIN_NAME
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    rate = 22050
    frames = bytearray()
    for tone, length in ((880, 0.18), (0, 0.06), (1175, 0.18), (0, 0.06)) * 2 + ((0, 0.3),):
        for i in range(int(rate * length)):
            fade = min(1.0, i / 200, (rate * length - i) / 200)  # avoid clicks
            value = int(12000 * fade * math.sin(2 * math.pi * tone * i / rate)) if tone else 0
            frames += struct.pack("<h", value)
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return target


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / float(audio.getframerate() or 1)
    except (OSError, wave.Error, EOFError):
        return 2.0


def _default_player(path: Path, stop: threading.Event) -> None:
    """Play one WAV synchronously (until it ends or ``stop`` is set)."""
    if sys.platform == "win32":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        stop.wait(wav_seconds(path))
        winsound.PlaySound(None, 0)
    else:
        sys.stdout.write("\a")
        sys.stdout.flush()
        stop.wait(wav_seconds(path))


class Alerter:
    """Rings when told to; at most one alarm at a time (a new one restarts it)."""

    def __init__(self, player: Callable[[Path, threading.Event], None] | None = None):
        self.player = player or _default_player
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.listeners: list[Callable[[bool, str], None]] = []  # (ringing, reason)
        self.last_reason = ""

    @property
    def ringing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def sound_file(self) -> Path:
        chosen = app_settings.current().alert_sound
        if chosen and Path(chosen).is_file() and chosen.lower().endswith(".wav"):
            return Path(chosen)
        return builtin_ringtone()

    def alert(self, reason: str, force: bool = False, sound: str | None = None,
              repeats: int | None = None) -> bool:
        """Ring (if alerts are on, or ``force`` for the Test button). False if not rung.

        ``sound`` / ``repeats`` override the saved settings (Settings' Test button plays
        what is chosen in the dialog before it is saved)."""
        current = app_settings.current()
        if not (current.alert_enabled or force):
            return False
        repeats = max(1, int(current.alert_repeats if repeats is None else repeats))
        try:
            if sound is not None:
                chosen = Path(sound) if sound and sound.lower().endswith(".wav") else None
                sound = chosen if chosen and chosen.is_file() else builtin_ringtone()
            else:
                sound = self.sound_file()
        except OSError as exc:
            log.warning("No alarm sound available: %s", exc)
            return False
        self.stop()
        with self._lock:
            self._stop = threading.Event()
            stop = self._stop
            self.last_reason = reason
            self._thread = threading.Thread(target=self._ring, args=(sound, repeats, stop, reason),
                                            name="alarm", daemon=True)
            self._thread.start()
        log.warning("Alarm: %s", reason)
        return True

    def _ring(self, sound: Path, repeats: int, stop: threading.Event, reason: str) -> None:
        self._notify(True, reason)
        try:
            for _ in range(repeats):
                if stop.is_set():
                    break
                self.player(sound, stop)
                if stop.wait(0.3):
                    break
        except Exception:  # a broken sound device must never break a run
            log.exception("Could not play the alarm sound")
        finally:
            self._notify(False, reason)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(2)

    def _notify(self, ringing: bool, reason: str) -> None:
        for listener in list(self.listeners):
            try:
                listener(ringing, reason)
            except Exception:
                log.exception("Alarm listener failed")


def wait_until_silent(alerter: Alerter, timeout: float = 5.0) -> None:
    """For tests: wait for the alarm thread to finish."""
    deadline = time.monotonic() + timeout
    while alerter.ringing and time.monotonic() < deadline:
        time.sleep(0.01)
