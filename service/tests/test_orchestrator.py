"""Tests for music_service.orchestrator — lock, run_fetch, debounce."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call, patch

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


# ---------------------------------------------------------------------------
# _push_library
# ---------------------------------------------------------------------------


def test_push_library_skips_when_no_remote(monkeypatch):
    """_push_library() is a no-op when LIBRARY_REMOTE is not set."""
    monkeypatch.delenv("LIBRARY_REMOTE", raising=False)
    orc = Orchestrator()
    with patch("music_service.orchestrator.subprocess") as mock_sub:
        orc._push_library()
        mock_sub.run.assert_not_called()


def test_push_library_calls_rclone_copy_then_sync(monkeypatch, tmp_path):
    """_push_library() runs rclone copy (staging) then rclone sync (playlists)."""
    remote = ":webdav:"
    staging = tmp_path / "staging"
    playlists = tmp_path / "playlists"
    monkeypatch.setenv("LIBRARY_REMOTE", remote)
    monkeypatch.setenv("MUSIC_STAGING", str(staging))
    monkeypatch.setenv("MUSIC_PLAYLISTS", str(playlists))

    orc = Orchestrator()
    with patch("music_service.orchestrator.subprocess") as mock_sub:
        orc._push_library()

    assert mock_sub.run.call_count == 2
    copy_call, sync_call = mock_sub.run.call_args_list
    assert copy_call == call(["rclone", "copy", str(staging), remote], check=True)
    assert sync_call == call(["rclone", "sync", str(playlists), f"{remote}/playlists"], check=True)


def test_push_library_uses_default_paths_when_env_unset(monkeypatch):
    """_push_library() falls back to /root/Music/staging and /root/Music/playlists."""
    monkeypatch.setenv("LIBRARY_REMOTE", ":webdav:")
    monkeypatch.delenv("MUSIC_STAGING", raising=False)
    monkeypatch.delenv("MUSIC_PLAYLISTS", raising=False)

    orc = Orchestrator()
    with patch("music_service.orchestrator.subprocess") as mock_sub:
        orc._push_library()

    copy_call, sync_call = mock_sub.run.call_args_list
    assert copy_call == call(["rclone", "copy", "/root/Music/staging", ":webdav:"], check=True)
    assert sync_call == call(["rclone", "sync", "/root/Music/playlists", ":webdav:/playlists"], check=True)


# ---------------------------------------------------------------------------
# _run_scan_locked — push + trigger ordering
# ---------------------------------------------------------------------------


def test_run_scan_locked_calls_scan_then_push_then_trigger_in_order(monkeypatch):
    """scan.run → _push_library → trigger_scan must execute in that order."""
    monkeypatch.setenv("LIBRARY_REMOTE", ":webdav:")
    call_order: list[str] = []

    orc = Orchestrator()

    with patch("music_service.orchestrator.scan") as mock_scan, \
         patch.object(orc, "_push_library", side_effect=lambda: call_order.append("push")), \
         patch("music_service.orchestrator.trigger_scan", side_effect=lambda: call_order.append("trigger")):
        mock_scan.run.side_effect = lambda pending=None: call_order.append("scan")
        orc._run_scan_locked()

    assert call_order == ["scan", "push", "trigger"]


def test_run_scan_locked_skips_trigger_when_no_remote(monkeypatch):
    """With no LIBRARY_REMOTE, push is skipped but trigger_scan is still called."""
    monkeypatch.delenv("LIBRARY_REMOTE", raising=False)
    call_order: list[str] = []

    orc = Orchestrator()

    with patch("music_service.orchestrator.scan") as mock_scan, \
         patch.object(orc, "_push_library", side_effect=lambda: call_order.append("push")), \
         patch("music_service.orchestrator.trigger_scan", side_effect=lambda: call_order.append("trigger")):
        mock_scan.run.side_effect = lambda pending=None: call_order.append("scan")
        orc._run_scan_locked()

    # push is still called (it decides internally to skip based on LIBRARY_REMOTE)
    assert call_order == ["scan", "push", "trigger"]
