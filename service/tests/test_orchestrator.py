"""Tests for music_service.orchestrator — lock, run_fetch, debounce."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from music_service.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Lock-contention tests
# ---------------------------------------------------------------------------


def test_try_run_fetch_returns_false_when_lock_held():
    orc = Orchestrator(debounce_delay=30.0)
    orc._lock.acquire()
    try:
        result = orc.try_run_fetch()
        assert result is False
    finally:
        orc._lock.release()


def test_try_run_scan_returns_false_when_lock_held():
    orc = Orchestrator(debounce_delay=30.0)
    orc._lock.acquire()
    try:
        result = orc.try_run_scan()
        assert result is False
    finally:
        orc._lock.release()


def test_try_run_fetch_returns_true_when_lock_free():
    orc = Orchestrator(debounce_delay=30.0)
    with patch("music_service.orchestrator.ingest") as mock_ingest, \
         patch("music_service.orchestrator.scan") as mock_scan:
        mock_ingest.run.return_value = MagicMock()
        result = orc.try_run_fetch()
        assert result is True
        # Give background thread time to finish
        time.sleep(0.1)
        mock_ingest.run.assert_called_once()


def test_try_run_scan_returns_true_when_lock_free():
    orc = Orchestrator(debounce_delay=30.0)
    with patch("music_service.orchestrator.scan") as mock_scan:
        result = orc.try_run_scan()
        assert result is True
        time.sleep(0.1)
        mock_scan.run.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# run_fetch → run_scan chaining
# ---------------------------------------------------------------------------


def test_run_fetch_calls_ingest_then_scan_with_pending():
    orc = Orchestrator(debounce_delay=30.0)
    mock_pending = MagicMock()

    with patch("music_service.orchestrator.ingest") as mock_ingest, \
         patch("music_service.orchestrator.scan") as mock_scan:
        mock_ingest.run.return_value = mock_pending
        orc.run_fetch()
        mock_ingest.run.assert_called_once()
        mock_scan.run.assert_called_once_with(mock_pending)


def test_run_scan_calls_scan_run():
    orc = Orchestrator(debounce_delay=30.0)
    mock_pending = MagicMock()

    with patch("music_service.orchestrator.scan") as mock_scan:
        orc.run_scan(mock_pending)
        mock_scan.run.assert_called_once_with(mock_pending)


def test_run_scan_no_pending():
    orc = Orchestrator(debounce_delay=30.0)
    with patch("music_service.orchestrator.scan") as mock_scan:
        orc.run_scan()
        mock_scan.run.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


def test_debounce_fires_once_after_quiet_period():
    orc = Orchestrator(debounce_delay=0.05)

    with patch("music_service.orchestrator.scan") as mock_scan, \
         patch("music_service.orchestrator.ingest"):
        # Trigger three rapid events — should only fire one scan
        orc._schedule_debounced_scan()
        orc._schedule_debounced_scan()
        orc._schedule_debounced_scan()

        # Wait for debounce to fire and scan to complete
        time.sleep(0.3)
        assert mock_scan.run.call_count == 1


def test_debounce_timer_resets_on_each_event():
    orc = Orchestrator(debounce_delay=0.1)

    fired_at: list[float] = []

    original_try_scan = orc.try_run_scan

    def recording_scan():
        fired_at.append(time.monotonic())
        return original_try_scan()

    orc.try_run_scan = recording_scan

    with patch("music_service.orchestrator.scan"):
        t0 = time.monotonic()
        orc._schedule_debounced_scan()
        time.sleep(0.05)
        # Second event before first timer fires — should reset
        orc._schedule_debounced_scan()
        time.sleep(0.3)

    # Exactly one scan should have fired, and it should be after the second event
    assert len(fired_at) == 1
    assert fired_at[0] >= t0 + 0.05 + 0.1 - 0.02  # second event + debounce delay


def test_schedule_scan_public_method():
    """Orchestrator.schedule_scan() is a public alias used by the upload endpoint."""
    orc = Orchestrator(debounce_delay=0.05)
    with patch.object(orc, "_schedule_debounced_scan") as mock:
        orc.schedule_scan()
        mock.assert_called_once()
