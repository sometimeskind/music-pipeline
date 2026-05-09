"""Debounce timer: resets on each event, fires callback after a quiet period."""

from __future__ import annotations

import threading
from typing import Callable


class Debouncer:
    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        self._delay = delay
        self._callback = callback
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def trigger(self) -> None:
        """Reset the timer; fires callback after the quiet period elapses."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._delay, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        self._callback()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
