"""Tests for music_service.flows — task/flow wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# Prefect runs tasks and flows directly when called without a live server.
# Use the test harness so state writes go to an in-memory SQLite backend.
@pytest.fixture(autouse=True, scope="module")
def prefect_test_env():
    from prefect.testing.utilities import prefect_test_harness
    with prefect_test_harness():
        yield


# ---------------------------------------------------------------------------
# Fetch tasks
# ---------------------------------------------------------------------------


def test_preflight_task_succeeds_when_no_reason():
    from music_service.flows import preflight_task
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.preflight.return_value = None
        preflight_task()
        mock_ingest.preflight.assert_called_once()


def test_preflight_task_raises_on_failure():
    from music_service.flows import preflight_task
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.preflight.return_value = "missing_cookies"
        with pytest.raises(RuntimeError, match="missing_cookies"):
            preflight_task()


def test_reconcile_playlists_task_returns_remove_sources():
    from music_service.flows import reconcile_playlists_task
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.reconcile_playlists.return_value = ["old-playlist"]
        result = reconcile_playlists_task()
        assert result == ["old-playlist"]


def test_spotdl_sync_task_returns_pending_and_pushes_metrics():
    from music_service.flows import spotdl_sync_task
    mock_pending = MagicMock()
    mock_metrics = MagicMock()
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.IngestMetrics", return_value=mock_metrics):
        mock_ingest.sync_playlists.return_value = mock_pending
        result = spotdl_sync_task([])
        assert result is mock_pending
        mock_metrics.push.assert_called_once()


def test_spotdl_sync_task_pushes_metrics_on_failure():
    from music_service.flows import spotdl_sync_task
    mock_metrics = MagicMock()
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.IngestMetrics", return_value=mock_metrics):
        mock_ingest.sync_playlists.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            spotdl_sync_task([])
        mock_metrics.push.assert_called_once()
        assert mock_metrics.success is False


# ---------------------------------------------------------------------------
# Scan tasks
# ---------------------------------------------------------------------------


def test_save_removals_task_skips_when_empty():
    from music_service.flows import save_removals_task
    mock_pending = MagicMock()
    mock_pending.tracks = []
    mock_pending.remove_sources = []
    with patch("music_service.flows.ingest") as mock_ingest:
        save_removals_task(mock_pending)
        mock_ingest.save_pending_removals.assert_not_called()


def test_save_removals_task_writes_when_non_empty():
    from music_service.flows import save_removals_task
    mock_pending = MagicMock()
    mock_pending.tracks = [MagicMock()]
    mock_pending.remove_sources = []
    with patch("music_service.flows.ingest") as mock_ingest:
        save_removals_task(mock_pending)
        mock_ingest.save_pending_removals.assert_called_once_with(mock_pending)


def test_apply_removals_task_skips_when_no_file():
    from music_service.flows import apply_removals_task
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan:
        mock_ingest.load_and_clear_pending_removals.return_value = None
        apply_removals_task()
        mock_scan.apply_pending_removals.assert_not_called()


def test_apply_removals_task_calls_apply_pending_removals():
    from music_service.flows import apply_removals_task
    mock_pending = MagicMock()
    mock_lib = MagicMock()
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_scan.library.MusicLibrary") as MockLib:
        mock_ingest.load_and_clear_pending_removals.return_value = mock_pending
        MockLib.return_value.__enter__ = lambda _: mock_lib
        MockLib.return_value.__exit__ = MagicMock(return_value=False)
        apply_removals_task()
        mock_scan.apply_pending_removals.assert_called_once()


def test_beet_import_task_returns_imported():
    from music_service.flows import beet_import_task
    with patch("music_service.flows.concurrency") as mock_concurrency, \
         patch("music_service.flows.scan") as mock_scan:
        mock_concurrency.return_value.__enter__.return_value = None
        mock_concurrency.return_value.__exit__.return_value = False
        mock_scan.run_inbox_import.return_value = [("Title", "Artist")]
        result = beet_import_task()
        assert result == [("Title", "Artist")]
        mock_scan.run_inbox_import.assert_called_once()
        mock_concurrency.assert_called_once_with("beet-import", occupy=1)


def test_quarantine_task_calls_quarantine():
    from music_service.flows import quarantine_task
    with patch("music_service.flows.scan") as mock_scan:
        quarantine_task()
        mock_scan.quarantine_inbox_leftovers.assert_called_once()


def test_asis_import_task_calls_asis_import():
    from music_service.flows import asis_import_task
    with patch("music_service.flows.scan") as mock_scan:
        asis_import_task()
        mock_scan.import_asis_from_quarantine.assert_called_once()


def test_regen_playlists_task_calls_regen():
    from music_service.flows import regen_playlists_task
    with patch("music_service.flows.scan") as mock_scan:
        regen_playlists_task()
        mock_scan.regen_playlists.assert_called_once()


def test_reconcile_task_calls_reconcile_all():
    from music_service.flows import reconcile_task
    with patch("music_service.flows.reconcile") as mock_reconcile:
        reconcile_task()
        mock_reconcile.reconcile_all.assert_called_once()


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def test_fetch_and_scan_flow_runs_fetch_steps_only():
    from music_service.flows import fetch_and_scan_flow
    call_order: list[str] = []
    mock_pending = MagicMock()
    mock_pending.tracks = []
    mock_pending.remove_sources = []
    mock_metrics = MagicMock()

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.IngestMetrics", return_value=mock_metrics):
        mock_ingest.preflight.side_effect = lambda: call_order.append("preflight")
        mock_ingest.reconcile_playlists.side_effect = lambda: (call_order.append("reconcile-playlists"), [])[1]
        mock_ingest.sync_playlists.side_effect = lambda *_: (call_order.append("spotdl-sync"), mock_pending)[1]

        fetch_and_scan_flow()

    assert call_order == ["preflight", "reconcile-playlists", "spotdl-sync"]
    mock_scan.run_inbox_import.assert_not_called()


def test_scan_flow_runs_all_scan_steps_in_order():
    from music_service.flows import scan_flow
    call_order: list[str] = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.reconcile") as mock_reconcile, \
         patch("music_service.flows.concurrency") as mock_concurrency:
        mock_concurrency.return_value.__enter__.return_value = None
        mock_concurrency.return_value.__exit__.return_value = False
        mock_ingest.load_and_clear_pending_removals.return_value = None
        mock_scan.run_inbox_import.side_effect = lambda: (call_order.append("beet-import"), [])[1]
        mock_scan.quarantine_inbox_leftovers.side_effect = lambda: (call_order.append("quarantine"), 0)[1]
        mock_scan.import_asis_from_quarantine.side_effect = lambda: (call_order.append("asis-import"), 0)[1]
        mock_scan.regen_playlists.side_effect = lambda: (call_order.append("regen-playlists"), {})[1]
        mock_reconcile.reconcile_all.side_effect = lambda: (call_order.append("reconcile-snapshots"), 0)[1]

        with patch("music_scan.process.run_beet_update") as mock_update, \
             patch("music_scan.navidrome.trigger_scan") as mock_navidrome:
            mock_update.side_effect = lambda: call_order.append("beet-update")
            mock_navidrome.side_effect = lambda: call_order.append("navidrome")
            scan_flow()

    assert call_order == [
        "beet-import",
        "quarantine",
        "asis-import",
        "beet-update",
        "regen-playlists",
        "navidrome",
        "reconcile-snapshots",
    ]
    mock_ingest.preflight.assert_not_called()
    mock_ingest.sync_playlists.assert_not_called()
