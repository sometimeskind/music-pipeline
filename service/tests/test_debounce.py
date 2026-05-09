"""Tests for music_service.debounce — Debouncer timer behaviour."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from music_service.debounce import Debouncer


def test_fires_callback_after_quiet_period():
    cb = MagicMock()
    d = Debouncer(delay=0.05, callback=cb)
    d.trigger()
    time.sleep(0.2)
    cb.assert_called_once()


def test_resets_on_rapid_events():
    cb = MagicMock()
    d = Debouncer(delay=0.1, callback=cb)
    # Three rapid triggers — should only fire once after the final one.
    d.trigger()
    d.trigger()
    d.trigger()
    time.sleep(0.4)
    assert cb.call_count == 1


def test_cancel_prevents_fire():
    cb = MagicMock()
    d = Debouncer(delay=0.1, callback=cb)
    d.trigger()
    d.cancel()
    time.sleep(0.3)
    cb.assert_not_called()


def test_timer_resets_delay_on_second_event():
    cb = MagicMock()
    fired_at: list[float] = []
    d = Debouncer(delay=0.1, callback=lambda: fired_at.append(time.monotonic()))

    t0 = time.monotonic()
    d.trigger()
    time.sleep(0.05)
    d.trigger()  # reset — should fire 0.1s after this, not after the first trigger
    time.sleep(0.3)

    assert len(fired_at) == 1
    # fired at least 0.05 + 0.1 - small margin after t0
    assert fired_at[0] >= t0 + 0.1
