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
        preflight_task(MagicMock())
        mock_ingest.preflight.assert_called_once()


def test_preflight_task_raises_on_failure():
    from music_service.flows import preflight_task
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.preflight.return_value = "missing_cookies"
        metrics = MagicMock(failure_reason="")
        with pytest.raises(RuntimeError, match="missing_cookies"):
            preflight_task(metrics)
        assert metrics.failure_reason == "missing_cookies"


def test_reconcile_playlists_task_returns_remove_sources():
    from music_service.flows import reconcile_playlists_task
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.reconcile_playlists.return_value = ["old-playlist"]
        result = reconcile_playlists_task(MagicMock())
        assert result == ["old-playlist"]


def test_spotdl_sync_task_returns_pending_and_counts_into_metrics():
    from music_service.flows import spotdl_sync_task
    mock_pending = MagicMock()
    metrics = MagicMock()
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_scan.library.MusicLibrary"):
        mock_ingest.sync_playlists.return_value = mock_pending
        result = spotdl_sync_task([], metrics)
        assert result is mock_pending
        assert mock_ingest.sync_playlists.call_args.args[1] is metrics
        metrics.push.assert_not_called()


def test_spotdl_sync_task_links_tracks_the_library_has(tmp_path):
    """The sync's in_library callback tags library items by Spotify ID (#187)."""
    from beets.library import Item

    from music_scan.library import MusicLibrary
    from music_service.flows import spotdl_sync_task

    db = tmp_path / "library.db"
    with MusicLibrary(db, tmp_path) as lib:
        item = Item(title="Song", artist="Artist", sources="liked", spotify_ids="A1")
        lib._lib.add(item)

    answers = []

    def fake_sync(_remove, _metrics, in_library=None):
        answers.append(in_library("later", {"url": "https://open.spotify.com/track/A1"}))
        answers.append(in_library("later", {"url": "https://open.spotify.com/track/ZZ"}))
        return MagicMock()

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan.LIBRARY_DB", db):
        mock_ingest.sync_playlists.side_effect = fake_sync
        spotdl_sync_task([], MagicMock(tracks_linked=1))

    assert answers == [True, False]
    with MusicLibrary(db, tmp_path) as lib:
        assert lib._lib.get_item(item.id).get("sources") == "liked,later"


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


def test_fetch_flow_runs_fetch_then_scan():
    from music_service.flows import fetch_and_scan_flow
    call_order: list[str] = []
    mock_pending = MagicMock()
    mock_pending.tracks = []
    mock_pending.remove_sources = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.reconcile") as mock_reconcile, \
         patch("music_scan.library.MusicLibrary"), \
         patch("music_service.flows.concurrency") as mock_concurrency, \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"):
        mock_concurrency.return_value.__enter__.return_value = None
        mock_concurrency.return_value.__exit__.return_value = False
        mock_ingest.preflight.side_effect = lambda: call_order.append("preflight")
        mock_ingest.reconcile_playlists.side_effect = lambda: (call_order.append("reconcile-playlists"), [])[1]
        mock_ingest.sync_playlists.side_effect = lambda *_, **__: (call_order.append("spotdl-sync"), mock_pending)[1]
        mock_ingest.load_and_clear_pending_removals.return_value = None
        mock_scan.run_inbox_import.side_effect = lambda: (call_order.append("beet-import"), [])[1]
        mock_reconcile.reconcile_all.return_value = 0

        fetch_and_scan_flow()

    assert call_order[:3] == ["preflight", "reconcile-playlists", "spotdl-sync"]
    assert "beet-import" in call_order
    mock_scan.run_inbox_import.assert_called_once()


def _flow_ingest_pushes(tmp_path, monkeypatch, *, preflight=None, linked=0):
    """Run fetch_and_scan_flow with only I/O stubbed: the real ingest_run, the real
    cookie file parsing and the real IngestMetrics.push, so the flow path itself is
    what's tested (#203).  Returns the music_ingest push bodies."""
    import music_fetch.ingest as ingest

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1792766198\tSAPISID\tvalue\n",
        encoding="utf-8",
    )
    pending = ingest.PendingRemovals(tracks=[], remove_sources=[])

    def fake_sync(_remove, metrics, in_library=None):
        metrics.tracks_linked += linked
        return pending

    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushes.append((body, job)))
    monkeypatch.setattr(ingest, "COOKIE_FILE", cookies)
    monkeypatch.setattr(ingest, "FAILURES_FILE", tmp_path / "failures.json")
    monkeypatch.setattr(ingest, "CONF_PATH", tmp_path / "playlists.conf")
    monkeypatch.setattr(ingest, "preflight", lambda: preflight)
    monkeypatch.setattr(ingest, "reconcile_playlists", lambda: [])
    monkeypatch.setattr(ingest, "sync_playlists", fake_sync)

    from music_service.flows import fetch_and_scan_flow
    with patch("music_service.flows.concurrency"), \
         patch("music_service.flows._run_scan_tasks"), \
         patch("music_scan.library.MusicLibrary"):
        if preflight:
            with pytest.raises(RuntimeError):
                fetch_and_scan_flow()
        else:
            fetch_and_scan_flow()
    return [body for body, job in pushes if job == "music_ingest"]


def test_fetch_flow_pushes_cookie_expiry_and_linked(tmp_path, monkeypatch):
    """The nightly flow, not just ingest.run(), pushes the expiry gauge and the
    linked count in one music_ingest push (#203)."""
    bodies = _flow_ingest_pushes(tmp_path, monkeypatch, linked=2)
    assert len(bodies) == 1
    assert "music_ingest_cookies_expiry_timestamp_seconds 1792766198" in bodies[0]
    assert "music_ingest_tracks_linked_total 2" in bodies[0]
    assert "music_ingest_last_run_success 1" in bodies[0]


def test_fetch_flow_pushes_cookie_expiry_when_preflight_fails(tmp_path, monkeypatch):
    bodies = _flow_ingest_pushes(tmp_path, monkeypatch, preflight="auth_spotify")
    assert len(bodies) == 1
    assert "music_ingest_cookies_expiry_timestamp_seconds 1792766198" in bodies[0]
    assert 'reason="auth_spotify"' in bodies[0]


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


def test_scan_flow_skips_when_pipeline_busy():
    """Scan exits immediately if the pipeline concurrency slot is taken."""
    from music_service.flows import scan_flow
    with patch("music_service.flows.concurrency") as mock_concurrency, \
         patch("music_service.flows.ingest") as mock_ingest:
        mock_concurrency.return_value.__enter__.side_effect = TimeoutError
        mock_concurrency.return_value.__exit__.return_value = False
        scan_flow()
        mock_ingest.load_and_clear_pending_removals.assert_not_called()


# ---------------------------------------------------------------------------
# Scan metrics (music-pipeline#173)
# ---------------------------------------------------------------------------


def _scan_mocks(mock_scan, mock_ingest):
    mock_ingest.load_and_clear_pending_removals.return_value = None
    mock_scan.run_inbox_import.return_value = [("A", "B"), ("C", "D")]
    mock_scan.quarantine_inbox_leftovers.return_value = 1
    mock_scan.import_asis_from_quarantine.return_value = 3
    mock_scan.regen_playlists.return_value = {}
    mock_scan.count_lossless_items.return_value = 0


def test_scan_pushes_metrics_on_success():
    from music_service.flows import _run_scan_tasks
    pushed: list = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.reconcile") as mock_reconcile, \
         patch("music_service.flows.concurrency"), \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"), \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        mock_reconcile.reconcile_all.return_value = 0
        _run_scan_tasks()

    (m,) = pushed
    assert m.success
    assert m.tracks_imported == 5
    assert m.quarantined_tracks == 1
    assert m.lossless_items == 0


def test_scan_pushes_metrics_on_failure():
    from music_service.flows import _run_scan_tasks
    pushed: list = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.concurrency"), \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        mock_scan.count_lossless_items.return_value = 4
        mock_scan.run_inbox_import.side_effect = RuntimeError("beet crashed")
        with pytest.raises(Exception):
            _run_scan_tasks()

    (m,) = pushed
    assert not m.success
    assert m.failure_reason == "unexpected_error"
    assert m.lossless_items == 4


def test_scan_reports_navidrome_failure_reason():
    from music_service.flows import _run_scan_tasks
    pushed: list = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.concurrency"), \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan", side_effect=RuntimeError("down")), \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        with pytest.raises(Exception):
            _run_scan_tasks()

    (m,) = pushed
    assert m.failure_reason == "navidrome_trigger_failed"


# ---------------------------------------------------------------------------
# Skip-if-busy lock against a real Prefect server (#174)
# ---------------------------------------------------------------------------


def _create_limit(name: str) -> None:
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.actions import GlobalConcurrencyLimitCreate
    with get_client(sync_client=True) as client:
        client.create_global_concurrency_limit(GlobalConcurrencyLimitCreate(name=name, limit=1))


def test_album_import_flow_grab_next_uses_search_overrides(tmp_path):
    """The grab after an import searches an override album too (#202)."""
    import music_fetch.albums as albums
    from music_service import flows

    conf = tmp_path / "album-overrides.conf"
    conf.write_text("glyph  webdings four tet\n", encoding="utf-8")
    state = albums.State()
    state.albums["glyph"] = {
        "status": albums.WANTED, "blocklist": [], "name": "☼⃝◞⊖◟", "artist": "⣎⡇", "tracks_count": 2,
        "playlists": {"later": [["One", "⣎⡇"], ["Two", "⣎⡇"]]},
    }
    prowlarr = MagicMock()
    prowlarr.search.return_value = []
    complete = MagicMock(return_value="done")

    with patch.object(albums, "OVERRIDES_FILE", conf), \
         patch.object(albums.State, "load", return_value=state), \
         patch.object(albums.State, "save"), \
         patch.object(albums, "push_metrics"), \
         patch.object(albums.Settings, "from_env", return_value=albums.Settings(mode="on")), \
         patch("music_fetch.usenet.Prowlarr", return_value=prowlarr), \
         patch("music_fetch.usenet.Sabnzbd"), \
         patch("music_scan.library.MusicLibrary"), \
         patch.object(flows, "_album_library_hooks", return_value=(lambda playlist, tracks: False, None, complete)), \
         patch.object(flows, "concurrency"):
        flows.album_import_flow("SABnzbd_nzo_1", True, "/downloads/x")

    assert prowlarr.search.call_args_list[0].args == ("webdings four tet",)


def test_skip_if_busy_acquires_a_free_limit():
    from prefect.concurrency.sync import concurrency
    from music_service.flows import SKIP_IF_BUSY_SECONDS
    _create_limit("skip-if-busy-free")
    with concurrency("skip-if-busy-free", occupy=1, timeout_seconds=SKIP_IF_BUSY_SECONDS):
        pass


def test_skip_if_busy_times_out_on_a_held_limit():
    from prefect.concurrency.sync import concurrency
    from music_service.flows import SKIP_IF_BUSY_SECONDS
    _create_limit("skip-if-busy-held")
    with concurrency("skip-if-busy-held", occupy=1):
        with pytest.raises(TimeoutError):
            with concurrency("skip-if-busy-held", occupy=1, timeout_seconds=SKIP_IF_BUSY_SECONDS):
                pass
