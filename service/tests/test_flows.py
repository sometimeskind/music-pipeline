"""Tests for music_service.flows — task/flow wiring."""

from __future__ import annotations

from contextlib import nullcontext
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


def test_reconcile_playlists_task_deletes_removed_playlists_and_queues_nothing():
    """homelab#1914: a playlist gone from playlists.conf loses its store and .m3u
    in reconcile_playlists itself; nothing is handed on to the sync."""
    from music_service.flows import reconcile_playlists_task
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.reconcile_playlists.return_value = ["old-playlist"]
        assert reconcile_playlists_task(MagicMock()) is None
        mock_ingest.reconcile_playlists.assert_called_once_with()


def test_reconcile_playlists_task_sweeps_navidrome_with_the_conf_playlists():
    """homelab#2541: after the reconcile, Navidrome's playlists imported from a
    removed .m3u are swept; the playlists.conf names are never swept."""
    from music_service.flows import reconcile_playlists_task
    mix = MagicMock(nosync=False)
    mix.name = "mix"
    with patch("music_service.flows.ingest") as mock_ingest, \
            patch("music_service.flows.load_playlists", return_value=[mix]), \
            patch("music_scan.navidrome.sweep_playlists", return_value=["keep"]) as mock_sweep:
        mock_ingest.CONF_PATH.exists.return_value = True
        mock_ingest.reconcile_playlists.return_value = ["keep"]
        reconcile_playlists_task(MagicMock())
    mock_sweep.assert_called_once_with(mock_ingest.PLAYLISTS_DIR, {"mix"})


def test_reconcile_playlists_task_survives_a_navidrome_failure():
    from music_service.flows import reconcile_playlists_task
    metrics = MagicMock(failure_reason="")
    with patch("music_service.flows.ingest") as mock_ingest, \
            patch("music_service.flows.load_playlists", return_value=[]), \
            patch("music_scan.navidrome.sweep_playlists",
                  side_effect=RuntimeError("Navidrome login failed")) as mock_sweep:
        mock_ingest.CONF_PATH.exists.return_value = True
        mock_ingest.reconcile_playlists.return_value = []
        assert reconcile_playlists_task(metrics) is None
    mock_sweep.assert_called_once()
    assert metrics.failure_reason == ""


def test_reconcile_playlists_task_skips_the_sweep_without_playlists_conf():
    """No playlists.conf, no reconcile: nothing says which playlists to keep."""
    from music_service.flows import reconcile_playlists_task
    with patch("music_service.flows.ingest") as mock_ingest, \
            patch("music_scan.navidrome.sweep_playlists") as mock_sweep:
        mock_ingest.CONF_PATH.exists.return_value = False
        mock_ingest.reconcile_playlists.return_value = []
        reconcile_playlists_task(MagicMock())
    mock_sweep.assert_not_called()


def test_reconcile_playlists_task_marks_a_failure():
    from music_service.flows import reconcile_playlists_task
    metrics = MagicMock(failure_reason="")
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.reconcile_playlists.side_effect = OSError("disk")
        with pytest.raises(OSError):
            reconcile_playlists_task(metrics)
    assert metrics.failure_reason == "reconcile_error"


def test_spotdl_sync_task_passes_metrics_and_the_sync_hooks():
    from music_service.flows import spotdl_sync_task
    metrics = MagicMock()
    hooks = {"resolves": MagicMock(), "held": frozenset({"Q"}), "in_library": MagicMock()}
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan.sync_hooks", return_value=nullcontext(hooks)):
        assert spotdl_sync_task(metrics) is None
    mock_ingest.sync_playlists.assert_called_once_with(metrics, **hooks)
    metrics.push.assert_not_called()


def test_spotdl_sync_task_resolves_without_tagging_holds_and_links(tmp_path):
    """The sync's hooks (homelab#1914): ``resolves`` matches a library item by
    Spotify ID without tagging it, ``held`` is the downloads waiting in the inbox
    or quarantine, and ``in_library`` tags the item with the playlist (#187)."""
    from beets.library import Item

    from music_scan.library import MusicLibrary
    from music_service.flows import spotdl_sync_task

    db = tmp_path / "library.db"
    with MusicLibrary(db, tmp_path) as lib:
        item = Item(title="Song", artist="Artist", sources="liked", spotify_ids="A1")
        lib._lib.add(item)

    seen = {}

    def fake_sync(_metrics, resolves, held=frozenset(), in_library=None):
        seen["held"] = held
        seen["resolves"] = [resolves({"url": "https://open.spotify.com/track/A1"}),
                            resolves({"url": "https://open.spotify.com/track/ZZ"})]
        with MusicLibrary(db, tmp_path) as lib:
            seen["after_resolves"] = lib._lib.get_item(item.id).get("sources")
        seen["linked"] = [in_library("later", {"url": "https://open.spotify.com/track/A1"}),
                          in_library("later", {"url": "https://open.spotify.com/track/ZZ"})]

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan.LIBRARY_DB", db), \
         patch("music_service.flows.scan.held_urls", return_value=frozenset({"Q"})) as held_urls:
        mock_ingest.sync_playlists.side_effect = fake_sync
        spotdl_sync_task(MagicMock(tracks_linked=1))

    assert seen == {"held": frozenset({"Q"}), "resolves": [True, False], "after_resolves": "liked",
                    "linked": [True, False]}
    from music_service.flows import scan
    held_urls.assert_called_once_with(scan.QUARANTINE, scan.SPOTDL_DIR)
    with MusicLibrary(db, tmp_path) as lib:
        assert lib._lib.get_item(item.id).get("sources") == "liked,later"


# ---------------------------------------------------------------------------
# Scan tasks
# ---------------------------------------------------------------------------


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
        mock_scan.regen_playlists.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def test_fetch_flow_runs_fetch_then_scan():
    from music_service.flows import fetch_and_scan_flow
    call_order: list[str] = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_scan.library.MusicLibrary"), \
         patch("music_service.flows.concurrency") as mock_concurrency, \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"):
        mock_concurrency.return_value.__enter__.return_value = None
        mock_concurrency.return_value.__exit__.return_value = False
        mock_ingest.preflight.side_effect = lambda: call_order.append("preflight")
        mock_ingest.reconcile_playlists.side_effect = lambda: (call_order.append("reconcile-playlists"), [])[1]
        mock_ingest.sync_playlists.side_effect = lambda *_, **__: call_order.append("spotdl-sync")
        mock_scan.sync_hooks.return_value = nullcontext({})
        mock_scan.check_request_downloads.side_effect = lambda: (call_order.append("verify-requests"), {})[1]
        mock_scan.run_inbox_import.side_effect = lambda: (call_order.append("beet-import"), [])[1]

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
    def fake_sync(metrics, **_hooks):
        metrics.tracks_linked += linked

    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushes.append((body, job)))
    monkeypatch.setattr(ingest, "COOKIE_FILE", cookies)
    monkeypatch.setattr(ingest, "CONF_PATH", tmp_path / "playlists.conf")
    monkeypatch.setattr(ingest, "preflight", lambda: preflight)
    monkeypatch.setattr(ingest, "reconcile_playlists", lambda: [])
    monkeypatch.setattr(ingest, "sync_playlists", fake_sync)

    from music_service.flows import fetch_and_scan_flow
    with patch("music_service.flows.concurrency"), \
         patch("music_service.flows._run_scan_tasks"), \
         patch("music_service.flows.scan.sync_hooks", return_value=nullcontext({})):
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
         patch("music_service.flows.concurrency") as mock_concurrency:
        mock_concurrency.return_value.__enter__.return_value = None
        mock_concurrency.return_value.__exit__.return_value = False
        mock_scan.run_inbox_import.side_effect = lambda: (call_order.append("beet-import"), [])[1]
        mock_scan.quarantine_inbox_leftovers.side_effect = lambda: (call_order.append("quarantine"), 0)[1]
        mock_scan.import_asis_from_quarantine.side_effect = lambda: (call_order.append("asis-import"), 0)[1]
        mock_scan.regen_playlists.side_effect = lambda metrics: (call_order.append("regen-playlists"), {})[1]

        with patch("music_scan.process.run_beet_update") as mock_update, \
             patch("music_scan.navidrome.trigger_scan") as mock_navidrome, \
             patch("music_scan.library.MusicLibrary"), \
             patch("music_scan.canon._records", return_value={}), \
             patch("music_scan.release.retire_merged",
                   side_effect=lambda lib, records, listed: call_order.append("retire-merged") or 0), \
             patch("music_scan.release.retire_held", side_effect=lambda lib: call_order.append("retire-held") or 0), \
             patch("music_scan.canon.after_scan") as mock_canon:
            mock_update.side_effect = lambda: call_order.append("beet-update")
            mock_navidrome.side_effect = lambda: call_order.append("navidrome")
            mock_canon.side_effect = lambda lib, since: call_order.append("canon-albums")
            scan_flow()

    assert call_order == [
        "verify-requests",  # homelab#2546: the request fallback's confirmed downloads import in this scan
        "beet-import",
        "quarantine",
        "asis-import",
        "retire-merged",  # homelab#2545: before retire_held, which then sees the new extras
        "retire-held",
        "canon-albums",
        "beet-update",
        "regen-playlists",
        "navidrome",
    ]  # no removals before the import, no snapshot reconcile after Navidrome (homelab#1914)
    mock_ingest.preflight.assert_not_called()
    mock_ingest.sync_playlists.assert_not_called()


def test_scan_flow_skips_when_pipeline_busy():
    """Scan exits immediately if the pipeline concurrency slot is taken."""
    from music_service.flows import scan_flow
    with patch("music_service.flows.concurrency") as mock_concurrency, \
         patch("music_service.flows._run_scan_tasks") as run_scan:
        mock_concurrency.return_value.__enter__.side_effect = TimeoutError
        mock_concurrency.return_value.__exit__.return_value = False
        scan_flow()
        run_scan.assert_not_called()


# ---------------------------------------------------------------------------
# Scan metrics (music-pipeline#173)
# ---------------------------------------------------------------------------


def _scan_mocks(mock_scan, mock_ingest):
    mock_scan.check_request_downloads.return_value = {}
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
         patch("music_service.flows.concurrency"), \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"), \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        _run_scan_tasks()

    (m,) = pushed
    assert m.success
    assert m.tracks_imported == 5
    assert m.quarantined_tracks == 1
    assert m.lossless_items == 0


def test_scan_verifies_request_downloads_and_pushes_their_rejects():
    """homelab#2546: the Prefect scan checks the request fallback's downloads, as music-scan's run() does."""
    from music_service.flows import _run_scan_tasks
    pushed: list = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.concurrency"), \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"), \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        mock_scan.check_request_downloads.return_value = {"acoustid-unknown": 2, "fingerprint": 1}
        _run_scan_tasks()

    mock_scan.check_request_downloads.assert_called_once_with()
    (m,) = pushed
    assert m.rejected == {"duration": 0, "silence": 0, "fingerprint": 1, "acoustid-unknown": 2}


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


def test_scan_reports_a_musicbrainz_outage():
    from music_scan.scan import MusicBrainzUnavailable
    from music_service.flows import _run_scan_tasks
    pushed: list = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.concurrency"), \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"), \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        mock_scan.run_inbox_import.side_effect = MusicBrainzUnavailable("503", [("A", "B")])
        with pytest.raises(MusicBrainzUnavailable):
            _run_scan_tasks()

    (m,) = pushed
    assert not m.success and m.failure_reason == "musicbrainz_unavailable"
    assert m.tracks_imported == 1 + 3  # the import's one, plus the asis pass's
    mock_scan.quarantine_inbox_leftovers.assert_not_called()
    # What did import still gets the rest of the scan.
    mock_scan.regen_playlists.assert_called_once()


def test_album_import_defers_on_a_musicbrainz_outage():
    """The album flow sees an outage as ImportDeferred, not a crash (#264)."""
    import music_fetch.albums as albums
    from music_scan.scan import MusicBrainzUnavailable
    from music_service import flows

    seen = []

    def fake_complete(state, completion, import_inbox, *args, **kw):
        with pytest.raises(albums.ImportDeferred) as exc:
            import_inbox()
        seen.append(exc.value)
        return albums.GRABBED

    with patch.object(albums, "complete", side_effect=fake_complete), \
         patch.object(flows, "_run_scan_tasks", side_effect=MusicBrainzUnavailable("503")), \
         patch.object(flows, "concurrency"):
        _, _, complete = flows._album_library_hooks(MagicMock())
        assert complete(albums.State(albums={}), albums.Completion(nzo_id="nzo_1", ok=True, path="x")) == albums.GRABBED

    assert len(seen) == 1 and isinstance(seen[0].__cause__, MusicBrainzUnavailable)


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
    prowlarr.blocked.return_value = []
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


def test_album_completion_wires_the_release_hooks():
    """#279: complete() gets the release's identification, the import of the files beets
    couldn't match, the claim and the retirement of other copies (playlists regenerated)."""
    import music_fetch.albums as albums
    from music_scan import release
    from music_service import flows

    lib = MagicMock()
    lib.all_items.return_value = []
    events, kw = [], {}

    def fake_complete(state, completion, import_inbox, claim, add_source, tag_ids, retire, **hooks):
        kw.update(hooks)
        events.append(("claim", claim("keep", 100.0, "K", "r1", "Job")))
        events.append(("retire", retire("K", {}, "r1", None)))
        events.append(("rest", hooks["import_rest"]("keep", "Job")))
        events.append(("identify", hooks["identify"]("/x")))
        return albums.IMPORTED

    title = MagicMock(title="Juicy")
    listed = MagicMock(name="listed")
    with patch.object(albums, "complete", side_effect=fake_complete), \
         patch.object(flows.scan, "store_listings", return_value=listed), \
         patch.object(release, "claim", return_value=[title]) as claim, \
         patch.object(release, "retire", return_value=2) as retire, \
         patch.object(release, "import_rest", return_value=1) as rest, \
         patch.object(release, "identify", return_value={"missing": 0}), \
         patch.object(flows.scan, "regen_playlists", side_effect=lambda: events.append("m3u")), \
         patch.object(flows, "concurrency"):
        _, _, complete = flows._album_library_hooks(lib)
        assert complete(albums.State(), albums.Completion(nzo_id="n", ok=True, path="x")) == albums.IMPORTED

    assert events == [("claim", ["Juicy"]), "m3u", ("retire", 2), ("rest", 1), ("identify", {"missing": 0})]
    assert claim.call_args.args[1:] == ("keep", 100.0, "K", "r1", "Job")
    # The stores' listings judge top-ups (homelab#1914), not ``sources``.
    assert retire.call_args.args[1:] == ("K", {}, "r1", None, listed)
    assert rest.call_args.args[2:] == ("keep", "Job")


def test_album_completion_supersedes_loose_copies_after_canon():
    """The album copy supersedes loose items after the IDs and canon (#266); the .m3u files follow.
    The release is this record's claimed items: another album's import in the same window is not
    (homelab#2531)."""
    import music_fetch.albums as albums
    from music_scan import canon, release, supersede
    from music_service import flows

    events = []
    lib = MagicMock()
    fresh, old, other = MagicMock(added=200.0), MagicMock(added=50.0), MagicMock(added=150.0)
    for item, record in ((fresh, "K"), (old, "K"), (other, "OTHER")):
        item.get.side_effect = lambda k, d=None, record=record: {"via": "usenet", "album_record": record}.get(k, d)
    lib.items_by_source.return_value = [fresh, old, other]
    with patch.object(flows.scan, "tag_album_ids", side_effect=lambda *a: events.append("ids")), \
         patch.object(release, "retire_held", side_effect=lambda lib: events.append("held") or 1), \
         patch.object(canon, "canonicalize_items", side_effect=lambda items: events.append("canon")), \
         patch.object(supersede, "after_import", side_effect=lambda lib, items, n, tracks: events.append(("sup", items, n)) or 1), \
         patch("music_scan.cover.embed_covers", return_value=0), \
         patch("music_scan.cover.covers_by_id", return_value={}), \
         patch.object(flows.scan, "regen_playlists", side_effect=lambda: events.append("m3u")), \
         patch("music_scan.navidrome.trigger_scan", side_effect=lambda: events.append("rescan")), \
         patch.object(flows, "concurrency"):
        hooks = flows._album_library_hooks(lib)
        with patch.object(albums, "complete",
                          side_effect=lambda state, c, imp, claim, add, tag, retire, **kw: tag("keep", [], 100.0, 10, "K")):
            hooks[2](albums.State(), albums.Completion(nzo_id="n", ok=True, path="x"))

    # The extras the new IDs show the release holds go before canon moves files (#294).
    assert events == ["ids", "held", "m3u", "canon", ("sup", [fresh], 10), "m3u", "rescan"]


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


def test_scan_purges_quarantine_replaced_and_pushes_the_counts():
    """#288: every scan purges quarantine/replaced/ under its pipeline lock."""
    from music_service.flows import _run_scan_tasks
    pushed: list = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.concurrency"), \
         patch("music_scan.process.run_beet_update"), \
         patch("music_scan.navidrome.trigger_scan"), \
         patch("music_scan.library.MusicLibrary"), \
         patch("music_scan.release.purge_replaced",
               return_value={"superseded": 3, "only_copy": 17, "purged": 5}) as purge, \
         patch("music_scan.metrics.ScanMetrics.push", autospec=True, side_effect=pushed.append):
        _scan_mocks(mock_scan, mock_ingest)
        _run_scan_tasks()

    assert purge.call_args.args[2] == 14
    (m,) = pushed
    assert m.replaced == {"superseded": 3, "only_copy": 17} and m.replaced_purged == 5


def test_album_mixed_hook_reads_the_library_once():
    from music_service import flows

    lib = MagicMock()
    item = {"spotify_album_id": "K", "via": "spotdl"}
    lib.all_items.return_value = [MagicMock(get=item.get)]
    with patch.object(flows.scan, "store_listings", return_value=lambda i: set()) as listings:
        mixed = flows._album_mixed_hook(lib)
        assert mixed("17", {"status": "imported", "spotify_album_ids": ["K"]}) == ["1 spotdl track(s) on the album"]
        assert mixed("18", {"status": "imported", "spotify_album_ids": ["OTHER"]}) == []
    lib.all_items.assert_called_once()
    listings.assert_called_once_with()  # the stores are read once too


def test_album_mixed_hook_leaves_a_copy_another_playlists_store_lists():
    """homelab#1914: a spotdl copy another playlist's store lists is loose, not a top-up."""
    from music_service import flows

    lib = MagicMock()
    item = {"spotify_album_id": "K", "via": "spotdl"}
    lib.all_items.return_value = [MagicMock(get=item.get)]
    with patch.object(flows.scan, "store_listings", return_value=lambda i: {"keep", "aaaaaah"}):
        mixed = flows._album_mixed_hook(lib)
        assert mixed("K", {"status": "imported", "playlists": {"keep": []}, "spotify_album_ids": ["K"]}) == []


def test_album_wants_hook_groups_the_want_list_by_album():
    """#324: the feed reads the nightly's want list (file holds applied), by album, with each one's first position."""
    from music_fetch import store
    from music_service import flows

    songs = [{"url": f"u{i}", "album_id": "A" if i < 3 else "B", "album_name": "X", "song_id": f"s{i}"}
             for i in range(6)]
    lib = MagicMock()
    lib.all_items.return_value = []
    with patch("music_scan.held.held_urls", return_value=frozenset({"u0"})), \
         patch.object(store, "load", return_value=MagicMock(songs=songs)):
        wants = flows._album_wants_hook(lib)
        assert [(k, first, [s["url"] for s in w]) for k, first, w in wants("disc")] == [
            ("A", 1, ["u1", "u2"]), ("B", 3, ["u3", "u4", "u5"])]
    lib.all_items.assert_called_once()


def test_album_library_feed_hook_reads_every_store_and_the_library():
    """homelab#2403: the library feed counts held items and every store's tracks, by Spotify album."""
    from music_scan import want_albums
    from music_service import flows

    held = MagicMock(id=1, path=b"/x")
    held.get.side_effect = lambda k, d=None: {"spotify_album_id": "A", "spotify_ids": "s0", "tracktotal": 9}.get(k, d)
    songs = [{"url": "u1", "album_id": "A", "album_name": "X", "song_id": "s1", "tracks_count": 10}]
    lib = MagicMock()
    lib.all_items.return_value = [held]
    with patch.object(want_albums, "read_stores", return_value={"disc": songs}):
        found = flows._album_library_feed_hook(lib)()
    assert found == [("A", 2, 10, False, {"disc": songs})]


def test_album_tick_task_writes_no_removal_queue():
    """homelab#1914: the tick writes the album playlists' stores; a song gone from
    Spotify leaves the next store, so nothing is queued for the scan to unlink."""
    import music_fetch.albums as albums
    from music_service import flows

    settings = albums.Settings(mode="on")
    playlist = MagicMock(album=True, url="https://open.spotify.com/playlist/P")
    playlist.name = "keep"
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.load_playlists", return_value=[playlist]), \
         patch("music_scan.library.MusicLibrary"), \
         patch("music_fetch.spotdl_ops.SpotifyPlaylists") as spotify, \
         patch("music_fetch.usenet.Prowlarr") as prowlarr, \
         patch("music_fetch.usenet.Sabnzbd") as sabnzbd, \
         patch.object(albums, "tick") as tick:
        tick.return_value = MagicMock(removed_songs={"keep": [{"url": "https://open.spotify.com/track/X"}]})
        flows.album_tick_task(settings)

    args = tick.call_args.args
    assert args[0] == [("keep", "https://open.spotify.com/playlist/P")]
    assert args[1:4] == (spotify.return_value, prowlarr.return_value, sabnzbd.return_value)
    assert callable(args[4]) and args[5] is settings and len(args) == 6
    assert set(tick.call_args.kwargs) == {"on_completion", "missing", "mixed", "want_playlists", "wants", "library",
                                          "request_songs", "recordings", "later_get"}
    assert tick.call_args.kwargs["want_playlists"] == []  # an album playlist has no want list to feed
    assert [c[0] for c in mock_ingest.method_calls] == []  # no schedule_unlink, no save_pending_removals


def test_album_request_recordings_reads_the_stamped_items_once():
    """homelab#2478: the tick's recordings hook, the request tracks an album's stamped items hold."""
    from music_service import flows

    def item(**fields):
        return MagicMock(get=lambda k, d=None: fields.get(k, d))

    lib = MagicMock()
    lib.all_items.return_value = [item(album_record="7", fallback_recording="r1", mb_trackid="r1"),
                                  item(album_record="7", mb_trackid="r2"), item(album_record="8", mb_trackid="r3"),
                                  item(mb_trackid="r4"), item(spotify_album_id="sp1", mb_trackid="r5")]
    recordings = flows._album_request_recordings(lib)
    assert recordings("7", {}) == {"r1", "r2"} and recordings("9", {}) == set()
    # An album playlist's copies, on its Spotify album, before the cutover made it a request's.
    assert recordings("9", {"spotify_album_ids": ["sp1"]}) == {"r5"}
    lib.all_items.assert_called_once()
