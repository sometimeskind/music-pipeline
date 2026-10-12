"""Tests for music_fetch.albums — playlist reduction, budget and the tick's state transitions."""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import music_fetch.albums as albums
from music_fetch.albums import Settings, State, reduce_to_albums, refresh_playlists, tick, top_up
from music_fetch.usenet import Release

MB = 1_000_000
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_push():
    with patch.object(albums, "_push"):
        yield


@pytest.fixture(autouse=True)
def _no_overrides(tmp_path: Path):
    with patch.object(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf"):
        yield


def song(name: str, album_id: str, album: str = "Album", artist: str = "Artist", url: str | None = None) -> dict:
    return {
        "name": name,
        "artists": [artist],
        "album_id": album_id,
        "album_name": album,
        "album_artist": artist,
        "tracks_count": 2,
        "year": 2020,
        "url": url or f"https://open.spotify.com/track/{album_id}-{name}",
    }


class FakeSpotify:
    def __init__(self, snapshot: str, songs: list[dict]) -> None:
        self.snapshot = snapshot
        self._songs = songs
        self.song_reads = 0

    def snapshot_id(self, url: str) -> str:
        return self.snapshot

    def songs(self, url: str) -> list[dict]:
        self.song_reads += 1
        return self._songs

    def album_songs(self, album_id: str) -> list[dict]:
        return []


def release(guid: str = "r1", title: str = "Artist-Album-WEB-FLAC-2020") -> Release:
    return Release(guid=guid, title=title, size=60 * MB, indexer_id=1,
                   download_url=f"http://prowlarr/1/download?id={guid}", categories=[3040])


def fakes(releases: list[Release] | None = None):
    prowlarr = MagicMock()
    prowlarr.search.return_value = [release()] if releases is None else releases
    prowlarr.blocked.return_value = []
    prowlarr.nzb_location.return_value = "https://indexer/getnzb?id=1"
    sab = MagicMock()
    sab.add_url.return_value = "SABnzbd_nzo_1"
    return prowlarr, sab


def wanted_state(n: int = 1) -> State:
    state = State()
    for i in range(n):
        state.albums[f"a{i}"] = {
            "status": albums.WANTED, "blocklist": [], "name": "Album", "artist": "Artist",
            "tracks_count": 2, "playlists": {"later": [["One", "Artist"], ["Two", "Artist"]]},
        }
    return state


def never_have(playlist, tracks) -> bool:
    return False


# ---------------------------------------------------------------------------
# reduce_to_albums / refresh_playlists
# ---------------------------------------------------------------------------


def test_reduce_to_albums_groups_in_playlist_order() -> None:
    result = reduce_to_albums([song("One", "B"), song("X", "A", album="Other"), song("Two", "B")])
    assert list(result) == ["B", "A"]
    assert [t[:2] for t in result["B"]["tracks"]] == [["One", "Artist"], ["Two", "Artist"]]


def test_reduce_to_albums_keeps_track_identity() -> None:
    """song_id, isrc and disc/track let the completion tag usenet items (#176)."""
    s = song("One", "B") | {"song_id": "sid1", "isrc": "GBUM71029604", "disc_number": 1, "track_number": 3}
    assert reduce_to_albums([s])["B"]["tracks"] == [["One", "Artist", "sid1", "GBUM71029604", 1, 3]]


def test_refresh_skips_unchanged_snapshot(tmp_path: Path) -> None:
    state = State(playlists={"later": {"snapshot_id": "s1"}})
    spotify = FakeSpotify("s1", [song("One", "A")])
    refresh_playlists(state, [("later", "url")], spotify, tmp_path, albums.TickResult())
    assert spotify.song_reads == 0
    assert state.albums == {}


def test_refresh_writes_spotdl_and_reports_removed_songs(tmp_path: Path) -> None:
    old = song("Gone", "Z", url="https://open.spotify.com/track/gone")
    (tmp_path / "later.spotdl").write_text(json.dumps({"type": "sync", "query": ["url"], "songs": [old]}))
    state = State()
    result = albums.TickResult()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s2", [song("One", "A")]), tmp_path, result)

    written = json.loads((tmp_path / "later.spotdl").read_text())
    assert [s["name"] for s in written["songs"]] == ["One"]
    assert result.removed_songs == {"later": [old]}
    assert state.albums["A"]["status"] == albums.WANTED
    assert state.playlists["later"]["snapshot_id"] == "s2"


def test_refresh_removing_one_of_an_albums_tracks_unlinks_nothing(tmp_path: Path) -> None:
    """#268: the album stays on the playlist while any of its tracks is."""
    one, two = song("One", "A"), song("Two", "A")
    state = State()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s1", [one, two]), tmp_path, albums.TickResult())
    result = albums.TickResult()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s2", [two]), tmp_path, result)
    assert result.removed_songs == {}
    assert "later" in state.albums["A"]["playlists"]


def test_refresh_removing_an_albums_last_track_unlinks_the_album(tmp_path: Path) -> None:
    """#268: the album's former songs are unlinked once none of its tracks remain."""
    one, two, other = song("One", "A"), song("Two", "A"), song("X", "B")
    state = State()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s1", [one, two, other]), tmp_path, albums.TickResult())
    refresh_playlists(state, [("later", "url")], FakeSpotify("s2", [two, other]), tmp_path, albums.TickResult())
    result = albums.TickResult()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s3", [other]), tmp_path, result)
    # The .spotdl held only Two by then; One's removal unlinked nothing before.
    assert result.removed_songs == {"later": [two]}
    assert "A" not in state.albums and "B" in state.albums


def test_refresh_one_new_track_makes_its_whole_album_wanted(tmp_path: Path) -> None:
    """#268: one song on Spotify brings in the album; have() judges the whole album."""
    state = State()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s1", [song("One", "A")]), tmp_path, albums.TickResult())
    record = state.albums["A"]
    assert record["status"] == albums.WANTED and record["tracks_count"] == 2
    asked = []
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, lambda key, rec: asked.append((key, rec["name"])) or False,
           albums.TickResult(), now=lambda: NOW)
    assert asked == [("A", "Album")] and record["status"] == albums.GRABBED


def test_refresh_drops_wanted_albums_no_longer_in_playlist(tmp_path: Path) -> None:
    state = State()
    refresh_playlists(state, [("later", "url")], FakeSpotify("s1", [song("One", "A"), song("X", "B")]), tmp_path, albums.TickResult())
    state.albums["B"]["status"] = albums.GRABBED
    refresh_playlists(state, [("later", "url")], FakeSpotify("s2", []), tmp_path, albums.TickResult())
    assert "A" not in state.albums
    # In flight: kept so the import trigger still finds it.
    assert state.albums["B"]["status"] == albums.GRABBED


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_counts_rolling_24h() -> None:
    state = State()
    state.record("grab", NOW - timedelta(hours=25))
    state.record("grab", NOW - timedelta(hours=1))
    assert state.used("grab", NOW) == 1
    state.record("hit", NOW)
    # record() prunes entries older than the window.
    assert len(state.indexer) == 2


def test_settings_reject_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("ALBUM_MODE", "yes")
    assert Settings.from_env().mode == "off"


# ---------------------------------------------------------------------------
# top_up
# ---------------------------------------------------------------------------


def test_dry_run_picks_without_grabbing() -> None:
    state = wanted_state()
    prowlarr, sab = fakes()
    result = albums.TickResult()
    top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, result, now=lambda: NOW)
    assert state.albums["a0"]["status"] == albums.DRY_RUN
    assert state.albums["a0"]["candidate"]["guid"] == "r1"
    prowlarr.nzb_location.assert_not_called()
    sab.add_url.assert_not_called()
    assert state.used("hit", NOW) == 1 and state.used("grab", NOW) == 0


def test_dry_run_is_capped_per_tick() -> None:
    state = wanted_state(5)
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="dry-run", dry_run_per_tick=2), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 2


def test_on_grabs_up_to_max_in_flight() -> None:
    state = wanted_state(5)
    state.albums["a0"]["status"] = albums.GRABBED
    prowlarr, sab = fakes()
    # Each album its own release: one another album has in flight waits (#243).
    prowlarr.search.side_effect = [[release(f"r{i}")] for i in range(1, 5)]
    result = albums.TickResult()
    top_up(state, Settings(mode="on", max_in_flight=3), prowlarr, sab, never_have, result, now=lambda: NOW)
    assert result.grabbed == 2
    assert state.in_flight() == 3
    assert state.albums["a1"]["nzo_id"] == "SABnzbd_nzo_1"
    sab.add_url.assert_called_with("https://indexer/getnzb?id=1", "Artist-Album-WEB-FLAC-2020")


def test_on_re_searches_dry_run_picks() -> None:
    state = wanted_state()
    state.albums["a0"]["status"] = albums.DRY_RUN
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["status"] == albums.GRABBED


def test_grab_budget_stops_grabbing() -> None:
    state = wanted_state(2)
    for _ in range(18):
        state.record("grab", NOW - timedelta(hours=1))
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_not_called()


def test_hit_budget_stops_searching() -> None:
    state = wanted_state(2)
    for _ in range(90):
        state.record("hit", NOW - timedelta(hours=1))
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_not_called()


def test_album_already_in_library_is_not_searched() -> None:
    state = wanted_state()
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, lambda pl, tracks: True, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["status"] == albums.HAVE
    prowlarr.search.assert_not_called()


def test_no_match_is_missing_until_retry() -> None:
    state = wanted_state()
    prowlarr, sab = fakes(releases=[])
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["status"] == albums.MISSING

    # Each search is the query, then the artist alone (#197).
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW + timedelta(days=1))
    assert prowlarr.search.call_count == 2
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW + timedelta(days=8))
    assert prowlarr.search.call_count == 4


def test_failed_redirect_blocklists_the_release() -> None:
    state = wanted_state()
    prowlarr, sab = fakes()
    prowlarr.nzb_location.side_effect = RuntimeError("404")
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["blocklist"] == ["r1"]
    assert state.albums["a0"]["status"] == albums.WANTED


def test_sabnzbd_failure_does_not_blocklist() -> None:
    state = wanted_state(2)
    prowlarr, sab = fakes()
    sab.add_url.side_effect = RuntimeError("down")
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["blocklist"] == []
    assert state.albums["a0"]["status"] == albums.WANTED
    # Stops at the first SABnzbd failure instead of spending grabs on the rest.
    assert prowlarr.search.call_count == 1


def test_search_error_stops_the_tick() -> None:
    state = wanted_state(2)
    prowlarr, sab = fakes()
    prowlarr.search.side_effect = RuntimeError("timeout")
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 1
    assert state.albums["a0"]["status"] == albums.WANTED


BACKOFF = [{"indexerId": 1, "disabledTill": "2026-10-04T12:15:00Z", "mostRecentFailure": "2026-10-04T11:55:00Z"}]


def test_indexer_in_backoff_stops_the_tick_before_any_search(caplog) -> None:
    state = wanted_state(3)
    prowlarr, sab = fakes(releases=[])
    prowlarr.blocked.return_value = BACKOFF
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_not_called()
    assert state.used("hit", NOW) == 0
    assert all(r["status"] == albums.WANTED and "searched_at" not in r for r in state.albums.values())
    assert caplog.text.count("[ERR]") == 1 and "indexer in Prowlarr backoff: 1 until 2026-10-04T12:15:00Z" in caplog.text


def test_empty_result_from_an_indexer_in_backoff_is_not_a_miss(caplog) -> None:
    """2026-10-10 (#263): the indexer goes into backoff mid-tick and Prowlarr
    answers 200 [] — no [MISS], no status change, no artist-only retry."""
    state = wanted_state(3)
    prowlarr, sab = fakes(releases=[])
    prowlarr.blocked.side_effect = [[], BACKOFF]
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 1 and state.used("hit", NOW) == 1
    assert all(r["status"] == albums.WANTED for r in state.albums.values())
    assert "[MISS]" not in caplog.text and caplog.text.count("[ERR]") == 1


def test_indexer_status_is_checked_once_while_searches_find_releases() -> None:
    state = wanted_state(3)
    prowlarr, sab = fakes()
    prowlarr.search.side_effect = [[release(f"r{i}")] for i in range(3)]
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.blocked.call_count == 1 and sab.add_url.call_count == 3


def test_indexer_status_failure_stops_the_tick() -> None:
    state = wanted_state(2)
    prowlarr, sab = fakes()
    prowlarr.blocked.side_effect = RuntimeError("connection refused")
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_not_called()
    assert state.albums["a0"]["status"] == albums.WANTED


def test_empty_results_in_a_row_stop_the_tick(caplog) -> None:
    state = wanted_state(5)
    prowlarr, sab = fakes(releases=[])
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert [r["status"] for r in state.albums.values()] == [albums.MISSING] * 2 + [albums.WANTED] * 3
    assert "3 albums in a row with 0 results" in caplog.text


def test_results_reset_the_empty_run() -> None:
    state = wanted_state(5)
    prowlarr, sab = fakes()
    nothing, other = [], [release(title="Other-Thing-WEB-FLAC-2020")]
    # Two queries per empty album (the artist-only retry), one when anything comes back.
    prowlarr.search.side_effect = [nothing, nothing, nothing, nothing, other, nothing, nothing, nothing, nothing]
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert all(r["status"] == albums.MISSING for r in state.albums.values())


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def test_tick_off_touches_nothing(tmp_path: Path) -> None:
    spotify = FakeSpotify("s1", [song("One", "A")])
    prowlarr, sab = fakes()
    tick([("later", "url")], spotify, prowlarr, sab, never_have, tmp_path, Settings(mode="off"), tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()
    assert spotify.song_reads == 0


def test_tick_persists_state(tmp_path: Path) -> None:
    prowlarr, sab = fakes()
    tick([("later", "url")], FakeSpotify("s1", [song("One", "A"), song("Two", "A")]), prowlarr, sab,
         never_have, tmp_path, Settings(mode="dry-run"), tmp_path / "state.json")
    saved = State.load(tmp_path / "state.json")
    assert saved.albums["A"]["status"] == albums.DRY_RUN
    assert saved.playlists["later"]["snapshot_id"] == "s1"


def test_tick_fails_on_a_spotify_rate_limit(tmp_path: Path) -> None:
    """A long 429 fails the tick (MusicAlbumTickFailing) instead of sleeping (#195)."""
    from music_fetch.spotify_limit import SpotifyRateLimited

    spotify = FakeSpotify("s1", [])
    spotify.snapshot_id = MagicMock(side_effect=SpotifyRateLimited(NOW, 62939))
    prowlarr, sab = fakes()
    with patch.object(albums, "push_metrics") as push, pytest.raises(SpotifyRateLimited):
        tick([("later", "url")], spotify, prowlarr, sab, never_have, tmp_path,
             Settings(mode="dry-run"), tmp_path / "state.json")
    push.assert_called_once()
    assert push.call_args.args[1] is False
    prowlarr.search.assert_not_called()


def test_dry_run_stops_at_total_sample_limit() -> None:
    state = wanted_state(10)
    prowlarr, sab = fakes()
    settings = Settings(mode="dry-run", dry_run_per_tick=3, dry_run_limit=4)
    for _ in range(3):
        top_up(state, settings, prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 4
    assert sum(1 for a in state.albums.values() if a.get("dry_run")) == 4


# ---------------------------------------------------------------------------
# complete / lost_completions
# ---------------------------------------------------------------------------


@pytest.fixture
def roots(tmp_path: Path):
    complete_root, inbox, quarantine = tmp_path / "complete", tmp_path / "inbox", tmp_path / "quarantine"
    job = complete_root / "album" / "Artist-Album-FLAC"
    job.mkdir(parents=True)
    (job / "01.flac").write_bytes(b"x")
    return complete_root, inbox, quarantine


def grabbed_state(playlists=None) -> State:
    state = wanted_state()
    record = state.albums["a0"]
    record.update({
        "status": albums.GRABBED, "nzo_id": "nzo1", "grabbed_at": albums._iso(NOW),
        "candidate": {"guid": "r1"},
    })
    if playlists:
        record["playlists"] = playlists
    return state


def _claim_all(playlist, since, key, guid, job):
    return ["One", "Two"]


def _no_retire(key, record, keep, drop):
    return 0


def run_complete(state, completion, roots, have_result=True, imported=None, tagged=None, titles=None,
                 identity=None, retired=None, claimed=None, rest=None):
    """*titles* are the titles of the release's items beets imported (default:
    the album's, or none when *have_result* is False); *identity* is the
    release's MusicBrainz match; *retired* collects ``(keep, drop)`` calls."""
    complete_root, inbox, quarantine = roots
    seen = imported if imported is not None else []
    added = []
    tagged = tagged if tagged is not None else []
    retired = retired if retired is not None else []
    claimed = claimed if claimed is not None else []
    if titles is None:
        titles = [t[0] for t in albums.album_tracks(next(iter(state.albums.values())))] if have_result else []

    def import_inbox():
        seen.extend(p.relative_to(inbox).as_posix() for p in inbox.rglob("*.flac"))

    def claim(playlist, since, key, guid, job):
        claimed.append((playlist, key, guid))
        return titles

    status = albums.complete(
        state, completion, import_inbox, claim,
        add_source=lambda have_src, new_src, tracks: added.append((have_src, new_src)),
        tag_ids=lambda pl, tracks, since, count: tagged.append((pl, count)),
        retire=lambda key, record, keep, drop: retired.append((keep, drop)) or 0,
        identify=(lambda folder: identity),
        import_rest=(lambda pl, job: (rest if rest is not None else []).append((pl, job)) or 0),
        complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
    )
    return status, seen, added


def test_complete_imports_from_the_playlist_inbox(roots) -> None:
    state = grabbed_state()
    status, seen, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots)
    assert status == albums.IMPORTED
    assert seen == ["later/Artist-Album-FLAC/01.flac"]
    complete_root, inbox, _ = roots
    # Moved out of SABnzbd's dir, and leftovers cleaned from the inbox.
    assert not (complete_root / "album" / "Artist-Album-FLAC").exists()
    assert not (inbox / "later" / "Artist-Album-FLAC").exists()


def test_complete_tags_the_other_playlists(roots) -> None:
    tracks = [["One", "Artist"]]
    state = grabbed_state(playlists={"later": tracks, "keep": tracks})
    _, _, added = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots)
    assert added == [("later", "keep")]


def test_complete_blocklists_when_beets_did_not_import_everything(roots) -> None:
    state = grabbed_state()
    _, _, quarantine = roots
    (quarantine / "later" / "Artist-Album-FLAC").mkdir(parents=True)
    status, _, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots, have_result=False)
    record = state.albums["a0"]
    assert status == albums.WANTED
    assert record["blocklist"] == ["r1"] and record["attempts"] == 1
    assert not (quarantine / "later" / "Artist-Album-FLAC").exists()


def test_complete_blocklists_a_failed_download_without_importing(roots) -> None:
    state = grabbed_state()
    status, seen, _ = run_complete(state, albums.Completion("nzo1", False, "album/Artist-Album-FLAC", "Repair failed"), roots)
    assert status == albums.WANTED and seen == []
    assert state.albums["a0"]["blocklist"] == ["r1"]
    complete_root, _, _ = roots
    assert not (complete_root / "album" / "Artist-Album-FLAC").exists()


def test_complete_gives_up_after_max_attempts_and_falls_back_to_spotdl(roots) -> None:
    """A failed album's tracks go to spotdl (#205)."""
    state = grabbed_state(playlists={"later": [["One", "Artist", "s1"], ["Two", "Artist", "s2"]]})
    state.albums["a0"]["attempts"] = albums.MAX_ATTEMPTS - 1
    status, _, _ = run_complete(state, albums.Completion("nzo1", False, "album/Artist-Album-FLAC"), roots,
                                have_result=False)
    record = state.albums["a0"]
    assert status == albums.FAILED and record["attempts"] == albums.MAX_ATTEMPTS
    # The next tick hands it to spotdl whole.
    albums.fallbacks(state, lambda pl, tracks: tracks)
    assert record["status"] == albums.FALLBACK and record["fallback_from"] == albums.FAILED
    assert record["fallback_tracks"] == {"later": ["s1", "s2"]}


def test_complete_ignores_jobs_it_did_not_grab(roots) -> None:
    state = grabbed_state()
    status, seen, _ = run_complete(state, albums.Completion("someone-else", True, "album/Artist-Album-FLAC"), roots)
    assert status is None and seen == []
    assert state.albums["a0"]["status"] == albums.GRABBED


def test_complete_rejects_a_path_outside_the_complete_dir(roots) -> None:
    state = grabbed_state()
    status, seen, _ = run_complete(state, albums.Completion("nzo1", True, "../../etc"), roots)
    assert status == albums.WANTED and seen == []



def test_complete_import_crash_keeps_the_release_for_a_retry(roots) -> None:
    """An exception in the import moves the job back and leaves the record grabbed (#261)."""
    complete_root, inbox, quarantine = roots
    state = grabbed_state()

    def crash():
        raise ValueError("No closing quotation")

    status = albums.complete(
        state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), crash,
        claim=_claim_all, add_source=lambda *a: None, retire=_no_retire, tag_ids=lambda *a: None,
        complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
    )
    record = state.albums["a0"]
    assert status == albums.GRABBED and record["status"] == albums.GRABBED
    assert record["blocklist"] == [] and record.get("attempts", 0) == 0
    assert (complete_root / "album" / "Artist-Album-FLAC" / "01.flac").exists()
    assert not (inbox / "later" / "Artist-Album-FLAC").exists()
    first_start = record["import_started"]

    # The next tick's recovery imports it, counting items from the first try.
    since = []
    status = albums.complete(
        state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), lambda: None,
        claim=_claim_all, add_source=lambda *a: None, retire=_no_retire,
        tag_ids=lambda pl, tracks, start, count: since.append(start),
        complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
    )
    assert status == albums.IMPORTED
    assert since == [first_start] and "import_started" not in record


def test_complete_missing_job_dir_grabs_again_without_an_attempt(roots, caplog) -> None:
    """A completed job whose dir is gone isn't the release's fault (#261)."""
    state = grabbed_state()
    state.albums["a0"]["attempts"] = albums.MAX_ATTEMPTS - 1
    with caplog.at_level("WARNING", logger="music_fetch.albums"):
        status, seen, _ = run_complete(state, albums.Completion("nzo1", True, "album/Gone"), roots)
    record = state.albums["a0"]
    assert status == albums.WANTED and seen == []
    assert record["blocklist"] == [] and record["attempts"] == albums.MAX_ATTEMPTS - 1
    assert "nzo_id" not in record
    assert "[LOST]" in caplog.text


def test_lost_completions_only_for_stale_grabs() -> None:
    state = grabbed_state()
    state.albums["a1"] = dict(state.albums["a0"], nzo_id="fresh", grabbed_at=albums._iso(NOW))
    state.albums["a0"]["grabbed_at"] = albums._iso(NOW - timedelta(hours=2))
    sab = MagicMock()
    sab.finished.return_value = {"nzo1": {"ok": True, "storage": "/downloads/complete/album/X", "fail_message": ""}}
    lost = albums.lost_completions(state, sab, now=NOW)
    sab.finished.assert_called_once_with(["nzo1"])
    assert lost == [albums.Completion("nzo1", True, "album/X", "")]


def test_tick_recovers_lost_triggers_only_when_on(tmp_path: Path) -> None:
    state = grabbed_state()
    state.albums["a0"]["grabbed_at"] = albums._iso(datetime.now(timezone.utc) - timedelta(hours=2))
    state.save(tmp_path / "state.json")
    prowlarr, sab = fakes()
    sab.finished.return_value = {"nzo1": {"ok": False, "storage": "", "fail_message": "x"}}
    calls = []
    for mode in ("dry-run", "on"):
        tick([], FakeSpotify("s", []), prowlarr, sab, never_have, tmp_path, Settings(mode=mode),
             tmp_path / "state.json", on_completion=lambda st, c: calls.append((mode, c.nzo_id)))
    assert calls == [("on", "nzo1")]


def test_complete_tags_ids_for_every_playlist(roots) -> None:
    tracks = [["One", "Artist", "sid1", None, 1, 1]]
    state = grabbed_state(playlists={"later": tracks, "keep": tracks})
    tagged = []
    run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots, tagged=tagged)
    assert tagged == [("later", 2), ("keep", 2)]


def test_complete_retires_the_other_copies_before_tagging(roots) -> None:
    """An old copy holding the canonical paths is gone before canon runs (#283)."""
    calls = []
    run_complete(grabbed_state(), albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                 tagged=calls, retired=calls)
    assert len(calls) == 2 and calls[0][0].startswith("r1@") and calls[1][0] == "later"


def test_complete_kept_incomplete_copy_is_tagged_after_retire(roots) -> None:
    calls = []
    run_complete(grabbed_state(), albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                 tagged=calls, retired=calls, identity={"missing": 1, "album": "Album", "mb_albumid": "mb1"})
    assert len(calls) == 2 and calls[0][0].startswith("r1@") and calls[1][0] == "later"


def test_complete_failed_download_tags_nothing(roots) -> None:
    tagged = []
    run_complete(grabbed_state(), albums.Completion("nzo1", False, "album/Artist-Album-FLAC"), roots, tagged=tagged)
    assert tagged == []


def test_reduce_to_albums_carries_album_type_and_whole_album_duration() -> None:
    """The matcher needs both (#189); a partial playlist's duration is scaled up."""
    s = song("One", "B") | {"album_type": "single", "duration": 200}
    album = reduce_to_albums([s])["B"]
    assert album["album_type"] == "single"
    assert album["duration"] == 400  # one of tracks_count=2 on the playlist


# ---------------------------------------------------------------------------
# Search plans: normalised queries, the artist-only retry, overrides (#197)
# ---------------------------------------------------------------------------

GLYPH_ARTIST = "⣎⡇ꉺლ༽இ•̛)ྀ◞ ༎ຶ ༽ৣৢ؞ৢ؞ؖ ꉺლ"
GLYPH_TITLE = "ʅ͡͡͡͡͡͡͡͡͡͡͡(̸̢̛̼̞̭͋ͅ)̸͚̰͛̔̾̀̿͒͂:̴͓̞̑̌̂̆̊͋̀ ҉● ࿀ ● l̡̡̡ ̡͌ Ɵʅ͡͡͡͡͡͡͡͡͡͡͡v̴̢͚͚͎ȯ̶̞̮͖̑̈́)̸̳̥̰̜̥̺̐ͅ ☼⃝◞⊖◟ ∷፨◉☼⃝"


def one_album(artist: str, name: str, key: str = "a0", **extra) -> State:
    state = State()
    state.albums[key] = {
        "status": albums.WANTED, "blocklist": [], "name": name, "artist": artist, "tracks_count": 2,
        "playlists": {"later": [["One", artist], ["Two", artist]]}, **extra,
    }
    return state


def test_query_is_normalised() -> None:
    state = one_album("Slayyyter", "WOR$T GIRL IN AMERICA (Deluxe)")
    prowlarr, sab = fakes([release(title="Slayyyter-WORsT GIRL IN AMERICA-24BIT-48KHZ-WEB-FLAC-2026-OBZEN")])
    top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_called_once_with("slayyyter worst girl in america")
    assert state.albums["a0"]["status"] == albums.DRY_RUN


def test_no_results_retries_the_artist_alone_and_counts_the_hit(caplog) -> None:
    state = one_album("Tierra Whack", "WHACK'S MUSEUM")
    prowlarr, sab = fakes()
    prowlarr.search.side_effect = [[], [release(title="Tierra Whack-WHACKS MUSEUM-24BIT-WEB-FLAC-2026-ENRiCH"),
                                        release("r2", "Tierra Whack-World Wide Whack-WEB-FLAC-2024-GRP")]]
    result = albums.TickResult()
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, result, now=lambda: NOW)
    assert [c.args[0] for c in prowlarr.search.call_args_list] == ["tierra whack whacks museum", "tierra whack"]
    assert state.used("hit", NOW) == 2 and result.searched == 2
    assert state.albums["a0"]["candidate"]["guid"] == "r1"


def test_results_that_do_not_match_are_not_retried() -> None:
    state = one_album("Artist", "Album")
    prowlarr, sab = fakes([release(title="Other-Thing-WEB-FLAC-2020")])
    top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 1


def test_retry_waits_when_the_budget_is_spent() -> None:
    state = one_album("Tierra Whack", "WHACK'S MUSEUM")
    for _ in range(89):
        state.record("hit", NOW - timedelta(hours=1))
    prowlarr, sab = fakes(releases=[])
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 1
    assert state.used("hit", NOW) == 90
    assert state.albums["a0"]["status"] == albums.WANTED


def test_miss_logs_every_query(caplog) -> None:
    state = one_album("Tierra Whack", "WHACK'S MUSEUM")
    prowlarr, sab = fakes(releases=[])
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert 'queries: "tierra whack whacks museum", "tierra whack"' in caplog.text
    assert state.albums["a0"]["status"] == albums.MISSING


def test_glyph_title_searches_the_artist_and_matches_any_album(caplog) -> None:
    state = one_album("Artist", GLYPH_TITLE)
    prowlarr, sab = fakes([release(title="Artist-Something Else-WEB-FLAC-2020")])
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_called_once_with("artist")
    assert state.albums["a0"]["status"] == albums.DRY_RUN
    assert "(artist-only) →" in caplog.text


def test_glyph_album_without_override_is_not_searched(caplog) -> None:
    state = one_album(GLYPH_ARTIST, GLYPH_TITLE, key="5glyphAlbumId")
    prowlarr, sab = fakes()
    with caplog.at_level("INFO"):
        top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_not_called()
    assert state.used("hit", NOW) == 0
    assert state.albums["5glyphAlbumId"]["status"] == albums.MISSING
    assert "[NOWORDS] 5glyphAlbumId:" in caplog.text and "add a search override" in caplog.text


def test_glyph_album_with_override(tmp_path: Path) -> None:
    conf = tmp_path / "album-overrides.conf"
    conf.write_text("# id  search\n5glyphAlbumId  webdings four tet  # glyph name\n\n", encoding="utf-8")
    overrides = albums.load_overrides(conf)
    assert overrides == {"5glyphAlbumId": "webdings four tet"}
    state = one_album(GLYPH_ARTIST, GLYPH_TITLE, key="5glyphAlbumId")
    prowlarr, sab = fakes([release(title="webdings-four-tet")])
    top_up(state, Settings(mode="dry-run"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW,
           overrides=overrides)
    prowlarr.search.assert_called_once_with("webdings four tet")
    assert state.albums["5glyphAlbumId"]["status"] == albums.DRY_RUN


def test_top_up_loads_overrides_when_not_given() -> None:
    """The import flow's grab-next calls top_up without overrides (#202)."""
    albums.OVERRIDES_FILE.write_text("5glyphAlbumId  webdings four tet\n", encoding="utf-8")
    state = one_album(GLYPH_ARTIST, GLYPH_TITLE, key="5glyphAlbumId")
    prowlarr, sab = fakes([release(title="webdings-four-tet")])
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    prowlarr.search.assert_called_once_with("webdings four tet")
    assert state.albums["5glyphAlbumId"]["status"] == albums.GRABBED


def test_missing_overrides_file_is_empty(tmp_path: Path) -> None:
    assert albums.load_overrides(tmp_path / "absent.conf") == {}


def test_changed_query_re_searches_a_miss_without_a_sample_slot() -> None:
    """A sampled miss from before #197 (no stored query) is re-checked once, even
    after the dry-run sample is complete, and then waits MISSING_RETRY again."""
    state = one_album("Slayyyter", "WOR$T GIRL IN AMERICA", status=albums.MISSING, dry_run=True,
                      searched_at=albums._iso(NOW - timedelta(hours=1)))
    prowlarr, sab = fakes(releases=[])
    settings = Settings(mode="dry-run", dry_run_limit=1)
    top_up(state, settings, prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 2  # the query, then the artist alone
    assert state.albums["a0"]["query"] == "slayyyter worst girl in america"
    top_up(state, settings, prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW + timedelta(hours=1))
    assert prowlarr.search.call_count == 2


def test_new_override_re_searches_a_noword_miss() -> None:
    state = one_album(GLYPH_ARTIST, GLYPH_TITLE, key="g")
    prowlarr, sab = fakes([release(title="webdings-four-tet")])
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["g"]["status"] == albums.MISSING
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(),
           now=lambda: NOW + timedelta(minutes=30), overrides={"g": "webdings four tet"})
    assert state.albums["g"]["status"] == albums.GRABBED


def test_various_artists_is_not_retried_alone() -> None:
    state = one_album("Various Artists", "Some Compilation")
    prowlarr, sab = fakes(releases=[])
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert prowlarr.search.call_count == 1


# ---------------------------------------------------------------------------
# Fallback to spotdl (#205)
# ---------------------------------------------------------------------------


def ten_tracks() -> list[list]:
    return [[f"T{i}", "Artist", f"s{i}"] for i in range(10)]


def full_tracks() -> list[list]:
    """Ten track entries with ISRC and disc/track, as reduce_to_albums writes them."""
    return [[f"T{i}", "Artist", f"s{i}", f"ISRC{i}", 1, i + 1] for i in range(10)]


def quarantined(roots, tags: dict[str, dict]):
    """Put the release's rejected files in its quarantine dir; returns a
    read_tags giving each file *tags*[name]."""
    job = roots[2] / "later" / "Artist-Album-FLAC"
    job.mkdir(parents=True, exist_ok=True)
    for name in tags:
        (job / name).write_bytes(b"x")
    (job / "Artist-Album.nfo").write_bytes(b"x")
    return lambda path: tags[path.name]


def test_reset_clears_the_rejected_tracks() -> None:
    state = fallback_state(albums.IMPORTED, {"later": full_tracks()})
    state.albums["a0"]["rejected_tracks"] = {"later": ["s0"]}
    assert albums.reset_album(state, "Album") is not None
    assert "rejected_tracks" not in state.albums["a0"]


def fallback_state(status: str, playlists: dict) -> State:
    state = State()
    state.albums["a0"] = {"status": status, "blocklist": [], "name": "Album", "artist": "Artist",
                          "tracks_count": 2, "playlists": playlists}
    return state


def album_song(n: int, album_id: str = "a0") -> dict:
    return {"name": f"T{n}", "artists": ["Artist"], "song_id": f"s{n}", "album_id": album_id,
            "disc_number": 1, "track_number": n}


def test_fallbacks_queue_a_missing_albums_whole_tracklist_then_fill_it() -> None:
    """#268: spotdl fetches the Spotify album's tracklist, not just the playlist's tracks."""
    state = fallback_state(albums.MISSING, {"later": [["T2", "Artist", "s2"]]})
    state.albums["a0"]["tracks_count"] = 3
    library: set[str | None] = {"s2"}
    reads = []

    def missing(pl, ts):
        return [t for t in ts if t[2] not in library]

    def album_songs(album_id):
        reads.append(album_id)
        return [album_song(1), album_song(2), album_song(3)]

    albums.fallbacks(state, missing, album_songs)
    record = state.albums["a0"]
    assert record["status"] == albums.FALLBACK and record["fallback_from"] == albums.MISSING
    assert record["fallback_tracks"] == {"later": ["s1", "s3"]}
    assert albums.fallback_queue(state) == 2
    assert albums.fallback_songs(state, "later", []) == [album_song(1), album_song(3)]

    library |= {"s1", "s3"}  # the nightly downloaded them and the scan imported them
    albums.fallbacks(state, missing, album_songs)
    assert record["status"] == albums.FILLED and "filled_at" in record
    assert albums.fallback_queue(state) == 0
    assert reads == ["a0"]  # read once


def test_fallbacks_never_fill_before_the_tracklist_is_read() -> None:
    state = fallback_state(albums.FALLBACK, {"later": [["T2", "Artist", "s2"]]})
    state.albums["a0"].update(fallback_from=albums.MISSING, fallback_tracks={})

    def broken(album_id):
        raise RuntimeError("429")

    albums.fallbacks(state, lambda pl, ts: [], broken)
    assert state.albums["a0"]["status"] == albums.FALLBACK and "album_songs" not in state.albums["a0"]


def test_fallbacks_read_a_capped_number_of_tracklists_per_tick() -> None:
    state = State()
    for i in range(albums.TRACKLISTS_PER_TICK + 2):
        state.albums[f"a{i}"] = {"status": albums.MISSING, "name": "Album", "artist": "Artist",
                                 "playlists": {"later": [["T1", "Artist", f"x{i}"]]}}
    reads = []
    albums.fallbacks(state, lambda pl, ts: ts, lambda album_id: reads.append(album_id) or [])
    assert len(reads) == albums.TRACKLISTS_PER_TICK
    # Not read yet: the playlist's tracks stand in.
    assert state.albums[f"a{albums.TRACKLISTS_PER_TICK}"]["fallback_tracks"] == {"later": [f"x{albums.TRACKLISTS_PER_TICK}"]}


def test_fallbacks_fetch_an_album_on_two_playlists_once_into_the_first() -> None:
    tracks = [["One", "Artist", "s1"]]
    state = fallback_state(albums.FAILED, {"later": tracks, "keep": tracks + [["Two", "Artist", "s2"]]})
    albums.fallbacks(state, lambda pl, ts: ts)
    assert state.albums["a0"]["fallback_tracks"] == {"later": ["s1", "s2"]}


def test_fallbacks_leave_other_statuses_alone() -> None:
    for status in (albums.WANTED, albums.GRABBED, albums.IMPORTED, albums.HAVE, albums.DRY_RUN):
        state = fallback_state(status, {"later": [["One", "Artist", "s1"]]})
        albums.fallbacks(state, lambda pl, ts: ts)
        assert state.albums["a0"]["status"] == status


# ---------------------------------------------------------------------------
# An original edition and its remaster share a release (#243)
# ---------------------------------------------------------------------------


def _holder(status: str, guid: str = "r1", **extra) -> dict:
    return {"status": status, "blocklist": [], "name": "Album", "artist": "Artist", "tracks_count": 2,
            "playlists": {"keep": [["One", "Artist"], ["Two", "Artist"]]}, "candidate": {"guid": guid}} | extra


def test_release_in_flight_for_another_album_waits() -> None:
    state = wanted_state()
    state.albums["orig"] = _holder(albums.GRABBED)
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["status"] == albums.WANTED
    assert "candidate" not in state.albums["a0"]
    sab.add_url.assert_not_called()


@pytest.mark.parametrize("holder", [
    _holder(albums.IMPORTED),
    _holder(albums.FALLBACK, fallback_from="partial"),
    _holder(albums.FILLED, fallback_from="partial"),
])
def test_release_another_album_imported_is_not_grabbed_again(holder) -> None:
    state = wanted_state()
    state.albums["orig"] = holder
    prowlarr, sab = fakes(releases=[release("r1"), release("r2", "Artist-Album-Deluxe-WEB-FLAC-2020")])
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["candidate"]["guid"] == "r2"
    assert state.albums["a0"]["status"] == albums.GRABBED


def test_release_a_failed_album_held_is_grabbed() -> None:
    state = wanted_state()
    state.albums["orig"] = _holder(albums.WANTED)
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["a0"]["candidate"]["guid"] == "r1"



def test_reset_album_undoes_the_last_attempt() -> None:
    """Zero 7's vinyl release was blocklisted for a deleted dir (#261): put it back."""
    state = grabbed_state()
    record = state.albums["a0"]
    record.update(status=albums.FALLBACK, attempts=3, blocklist=["r0", "r1"], fallback_from=albums.FAILED,
                  fallback_tracks={"later": ["s1"]})
    key, before, after = albums.reset_album(state, record["name"][:4].upper())
    assert key == "a0" and before["attempts"] == 3
    assert after["status"] == albums.WANTED and after["attempts"] == 2 and after["blocklist"] == ["r0"]
    assert "fallback_tracks" not in after and "nzo_id" not in after


def test_reset_album_needs_exactly_one_match() -> None:
    state = grabbed_state()
    state.albums["a1"] = dict(state.albums["a0"])
    assert albums.reset_album(state, state.albums["a0"]["name"]) is None
    assert albums.reset_album(state, "no such album") is None



def test_complete_an_import_that_always_crashes_counts_against_the_release(roots) -> None:
    """The retry is bounded: the third crash blocklists like a failed import (#261)."""
    complete_root, inbox, quarantine = roots
    state = grabbed_state()

    def crash():
        raise ValueError("boom")

    statuses = [
        albums.complete(
            state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), crash,
            claim=_claim_all, add_source=lambda *a: None, retire=_no_retire, tag_ids=lambda *a: None,
            complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
        )
        for _ in range(albums.MAX_IMPORT_ERRORS)
    ]
    record = state.albums["a0"]
    assert statuses == [albums.GRABBED] * (albums.MAX_IMPORT_ERRORS - 1) + [albums.WANTED]
    assert record["blocklist"] == ["r1"] and record["attempts"] == 1
    assert "import_errors" not in record and "import_started" not in record


def demoted_state() -> State:
    state = State()
    base = {"blocklist": [], "name": "Album", "artist": "Artist", "tracks_count": 2,
            "playlists": {"later": [["One", "Artist", "s1"], ["Two", "Artist", "s2"]]}}
    state.albums["old"] = base | {"status": albums.FALLBACK, "fallback_from": albums.MISSING,
                                  "fallback_at": "2026-10-01T00:00:00+00:00", "fallback_tracks": {"later": ["s1", "s2"]}}
    state.albums["new"] = base | {"status": albums.FALLBACK, "fallback_from": albums.MISSING,
                                  "fallback_at": "2026-10-10T23:15:02+00:00", "fallback_tracks": {"later": ["s2"]}}
    state.albums["failed"] = base | {"status": albums.FALLBACK, "fallback_from": albums.FAILED,
                                     "fallback_at": "2026-10-10T23:15:02+00:00", "fallback_tracks": {"later": ["s1"]}}
    state.albums["filled"] = base | {"status": albums.FILLED, "fallback_from": albums.MISSING,
                                     "fallback_at": "2026-10-10T23:15:02+00:00", "fallback_tracks": {}}
    return state


def test_requeue_since_puts_demoted_misses_back_to_wanted() -> None:
    state = demoted_state()
    found = albums.requeue(state, since=datetime(2026, 10, 10, 22, tzinfo=timezone.utc))
    assert [key for key, _ in found] == ["new"]
    assert found[0][1]["fallback_tracks"] == {"later": ["s2"]}
    record = state.albums["new"]
    assert record["status"] == albums.WANTED
    assert not {"fallback_from", "fallback_at", "fallback_tracks"} & record.keys()
    assert albums.fallback_queue(state) == 3  # old and failed still wait for spotdl
    assert albums.fallback_songs(state, "later", [{"song_id": "s2"}]) == [{"song_id": "s2"}]  # via old
    assert state.albums["old"]["status"] == state.albums["failed"]["status"] == albums.FALLBACK


def test_requeue_by_key_skips_albums_a_miss_did_not_demote(caplog) -> None:
    state = demoted_state()
    found = albums.requeue(state, keys=["old", "failed", "filled", "nope"])
    assert [key for key, _ in found] == ["old"]
    assert state.albums["failed"]["status"] == albums.FALLBACK
    assert state.albums["filled"]["status"] == albums.FILLED
    assert caplog.text.count("not an album a miss demoted") == 3


def test_requeued_album_is_searched_by_the_next_tick() -> None:
    state = demoted_state()
    albums.requeue(state, keys=["new"])
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW)
    assert state.albums["new"]["status"] == albums.GRABBED


def test_requeue_main_is_a_dry_run_without_apply(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "albums.json"
    demoted_state().save(path)
    monkeypatch.setattr(albums, "STATE_FILE", path)
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["music-album-requeue", "--since", "2026-10-10T22:00"])
    albums.requeue_main()
    assert State.load(path).albums["new"]["status"] == albums.FALLBACK
    monkeypatch.setattr("sys.argv", ["music-album-requeue", "--since", "2026-10-10T22:00", "--apply"])
    albums.requeue_main()
    assert State.load(path).albums["new"]["status"] == albums.WANTED


def test_complete_musicbrainz_outage_retries_without_blame(roots, caplog) -> None:
    """Yeezus (#264): a MusicBrainz outage during the import is not the release's fault."""
    complete_root, inbox, quarantine = roots
    state = grabbed_state()

    def outage():
        raise albums.ImportDeferred("MusicBrainz unavailable (503)")

    with caplog.at_level("INFO", logger="music_fetch.albums"):
        status = albums.complete(
            state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), outage,
            claim=_claim_all, add_source=lambda *a: None, retire=_no_retire, tag_ids=lambda *a: None,
            complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
        )
    record = state.albums["a0"]
    assert status == albums.GRABBED and record["status"] == albums.GRABBED
    assert record["blocklist"] == [] and record.get("attempts", 0) == 0
    assert record["import_deferrals"] == 1 and "import_errors" not in record
    assert (complete_root / "album" / "Artist-Album-FLAC" / "01.flac").exists()
    assert "[RETRY]" in caplog.text and "[FAIL]" not in caplog.text


def test_complete_musicbrainz_outage_does_not_spend_the_crash_retries(roots) -> None:
    """Deferrals have their own, longer cap: an outage outlasts three ticks (#264)."""
    complete_root, inbox, quarantine = roots
    state = grabbed_state()

    def outage():
        raise albums.ImportDeferred("MusicBrainz unavailable (503)")

    statuses = [
        albums.complete(
            state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), outage,
            claim=_claim_all, add_source=lambda *a: None, retire=_no_retire, tag_ids=lambda *a: None,
            complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
        )
        for _ in range(albums.MAX_IMPORT_DEFERRALS)
    ]
    record = state.albums["a0"]
    assert albums.MAX_IMPORT_DEFERRALS > albums.MAX_IMPORT_ERRORS
    assert statuses == [albums.GRABBED] * albums.MAX_IMPORT_DEFERRALS
    assert record["blocklist"] == [] and record.get("attempts", 0) == 0

    # MusicBrainz is back: the import succeeds and the counters go.
    status = albums.complete(
        state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), lambda: None,
        claim=_claim_all, add_source=lambda *a: None, retire=_no_retire, tag_ids=lambda *a: None,
        complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
    )
    assert status == albums.IMPORTED
    assert "import_deferrals" not in record and "import_started" not in record


def test_complete_an_endless_outage_counts_against_the_release(roots) -> None:
    """The cap: one deferral past MAX_IMPORT_DEFERRALS blocklists like a failed import (#264)."""
    complete_root, inbox, quarantine = roots
    state = grabbed_state()
    state.albums["a0"]["import_deferrals"] = albums.MAX_IMPORT_DEFERRALS

    def outage():
        raise albums.ImportDeferred("MusicBrainz unavailable (503)")

    status = albums.complete(
        state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), outage,
        claim=_claim_all, add_source=lambda *a: None, retire=_no_retire, tag_ids=lambda *a: None,
        complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
    )
    record = state.albums["a0"]
    assert status == albums.WANTED
    assert record["blocklist"] == ["r1"] and record["attempts"] == 1
    assert "import_deferrals" not in record


# ---------------------------------------------------------------------------
# The album is the unit (#279)
# ---------------------------------------------------------------------------

RTD = ["Intro", "Things Done Changed", "Gimme the Loot", "Machine Gun Funk", "Warning", "Ready to Die",
       "One More Chance", "#!*@ Me (Interlude)", "The What", "Juicy", "Everyday Struggle", "Me & My Bitch",
       "Big Poppa", "Respect", "Friend of Mine", "Unbelievable", "Suicidal Thoughts"]
RTD_REMASTER = [*RTD[:12], "Who Shot Ya?", *RTD[12:], "Just Playing (Dreams)"]


def ready_to_die() -> State:
    """Spotify's 19-track *Ready to Die (The Remaster)*, grabbed as the 1994 CD."""
    tracks = [[f"{t} - 2005 Remaster", "The Notorious B.I.G.", f"s{n}", None, 1, n]
              for n, t in enumerate(RTD_REMASTER, 1)]
    state = grabbed_state(playlists={"keep": tracks})
    state.albums["a0"].update(name="Ready to Die (The Remaster)", artist="The Notorious B.I.G.", tracks_count=19,
                              candidate={"guid": "r1", "title": "The_Notorious_BIG-Ready_To_Die-CD-FLAC-1994"})
    return state


def test_ready_to_die_cd_is_the_album_whole(roots, caplog) -> None:
    """2026-10-11: the 1994 CD had 7 tracks rolled back and the LP grabbed on top.
    Now it is imported whole, nothing to spotdl, nothing quarantined."""
    state = ready_to_die()
    retired, tagged, rest = [], [], []
    with caplog.at_level("INFO", logger="music_fetch.albums"):
        status, _, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                                    titles=RTD, retired=retired, tagged=tagged, rest=rest,
                                    identity={"mb_albumid": "cd94", "album": "Ready to Die", "missing": 0,
                                              "mb": {"mb_albumid": "cd94"}})
    record = state.albums["a0"]
    assert status == albums.IMPORTED and record["blocklist"] == [] and not record.get("attempts")
    assert [(k and k.split("@")[0], d) for k, d in retired] == [("r1", None)]  # every other copy goes
    assert rest == [("keep", "Artist-Album-FLAC")] and tagged == [("keep", 19)]
    assert record["release_files"]["mb_albumid"] == "cd94"
    assert "rejected_tracks" not in record and "fallback_tracks" not in record
    assert albums.fallback_queue(state) == 0
    assert "[DONE]" in caplog.text and "2 of Spotify's 19 titles not in this release" in caplog.text


def test_another_album_fails_the_title_check(roots, caplog) -> None:
    state = ready_to_die()
    retired = []
    with caplog.at_level("INFO", logger="music_fetch.albums"):
        status, _, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                                    titles=["Hypnotize", "Mo Money Mo Problems", "Juicy"], retired=retired)
    record = state.albums["a0"]
    assert status == albums.WANTED and record["blocklist"] == ["r1"] and record["attempts"] == 1
    assert [(k, d.split("@")[0]) for k, d in retired] == [(None, "r1")]  # the release's own items go
    assert "[WRONG]" in caplog.text


def test_wrong_album_check_skips_short_records() -> None:
    assert not albums.wrong_album(0, 3)
    assert albums.wrong_album(1, 4) and not albums.wrong_album(2, 4)


def test_an_incomplete_copy_is_kept_and_the_same_edition_grabbed_again(roots, caplog) -> None:
    state = ready_to_die()
    retired = []
    identity = {"mb_albumid": "cd94", "album": "Ready to Die", "missing": 1}
    with caplog.at_level("INFO", logger="music_fetch.albums"):
        status, _, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                                    titles=RTD[:-1], retired=retired, identity=identity)
    record = state.albums["a0"]
    assert status == albums.WANTED and record["blocklist"] == ["r1"] and record["attempts"] == 1
    best = record["best_copy"]
    assert (best["guid"], best["title"], best["missing"]) == ("r1", "The_Notorious_BIG-Ready_To_Die-CD-FLAC-1994", 1)
    assert best["release"].startswith("r1@")
    assert record["edition"] == []  # the CD names no edition: only another plain copy replaces it
    assert retired == [(best["release"], None)]  # kept for now; older copies go
    assert "[INCOMPLETE]" in caplog.text

    # The next copy of that edition is whole: it is the album, the incomplete one goes.
    record.update(status=albums.GRABBED, nzo_id="nzo2", candidate={"guid": "r2", "title": "BIG-Ready_To_Die-CD-1994"})
    (roots[0] / "album" / "Two").mkdir(parents=True)
    (roots[0] / "album" / "Two" / "01.flac").write_bytes(b"x")
    status, _, _ = run_complete(state, albums.Completion("nzo2", True, "album/Two"), roots, titles=RTD,
                                retired=retired, identity={"mb_albumid": "cd94", "album": "Ready to Die",
                                                           "missing": 0})
    assert status == albums.IMPORTED and retired[-1][0].startswith("r2@")
    assert "best_copy" not in record and "edition" not in record


def test_every_copy_incomplete_keeps_the_most_complete(roots, caplog) -> None:
    state = ready_to_die()
    record = state.albums["a0"]
    record.update(attempts=albums.MAX_ATTEMPTS - 1, blocklist=["r0"],
                  best_copy={"guid": "r0", "title": "older", "missing": 3})
    retired = []
    with caplog.at_level("INFO", logger="music_fetch.albums"):
        status, _, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                                    titles=RTD, retired=retired, identity={"album": "Ready to Die", "missing": 1})
    assert status == albums.IMPORTED and retired[0][0].startswith("r1@")  # r1 lacks less: r0 goes
    assert "lacks 1 track(s) of its own release" in caplog.text

    # A worse copy than the one kept: it goes instead.
    state = ready_to_die()
    state.albums["a0"]["best_copy"] = {"guid": "r0", "title": "older", "missing": 1}
    retired = []
    (roots[0] / "album" / "Artist-Album-FLAC").mkdir(parents=True)
    (roots[0] / "album" / "Artist-Album-FLAC" / "01.flac").write_bytes(b"x")
    run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots, titles=RTD,
                 retired=retired, identity={"album": "Ready to Die", "missing": 2})
    assert retired == [("r0", None)] and state.albums["a0"]["best_copy"]["guid"] == "r0"


def test_an_unmatched_file_in_a_complete_release_is_not_missing(roots) -> None:
    """A file beets can't match is imported as it is (import_rest) and counts for nothing."""
    state = ready_to_die()
    rest = []
    status, _, _ = run_complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                                titles=RTD, rest=rest,
                                identity={"album": "Ready to Die", "missing": 0, "unmatched": 1})
    assert status == albums.IMPORTED and rest == [("keep", "Artist-Album-FLAC")]


def test_no_more_copies_of_the_edition_keeps_the_incomplete_one() -> None:
    state = wanted_state()
    record = state.albums["a0"]
    record.update(edition=["remaster"], best_copy={"guid": "r0", "title": "Artist-Album-REMASTERED", "missing": 1},
                  blocklist=["r0"])
    prowlarr, sab = fakes([release("r2", "Artist-Album-WEB-FLAC-2020")])  # another edition only
    top_up(state, Settings(mode="on"), prowlarr, sab, lambda key, r: True, albums.TickResult(), now=lambda: NOW)
    assert record["status"] == albums.IMPORTED and "best_copy" not in record
    sab.add_url.assert_not_called()


def test_a_copy_of_the_same_edition_replaces_the_incomplete_one() -> None:
    state = wanted_state()
    record = state.albums["a0"]
    record.update(edition=["remaster"], best_copy={"guid": "r0", "title": "Artist-Album-REMASTERED", "missing": 1},
                  blocklist=["r0"])
    prowlarr, sab = fakes([release("r2", "Artist-Album-WEB-FLAC-2020"),
                           release("r3", "Artist-Album-REMASTERED-WEB-FLAC-2020")])
    # The library "has" the album through the kept copy: that doesn't stop the search.
    top_up(state, Settings(mode="on"), prowlarr, sab, lambda key, r: True, albums.TickResult(), now=lambda: NOW)
    assert record["status"] == albums.GRABBED and record["candidate"]["guid"] == "r3"


def test_reset_reimport_starts_over_from_one_release() -> None:
    """The operator's reset of an album mixed from several releases (#279)."""
    state = grabbed_state()
    record = state.albums["a0"]
    record.update(status=albums.IMPORTED, attempts=2, blocklist=["r0", "r1"], gaps=["s2"],
                  rejected_tracks={"later": ["s2"]}, filled_at="2026-10-11T00:00:00+00:00")
    _, _, after = albums.reset_album(state, "Album", reimport=True)
    assert after["status"] == albums.WANTED and after["blocklist"] == [] and after["attempts"] == 0
    assert after["reimport"] is True and "gaps" not in after and "filled_at" not in after
    prowlarr, sab = fakes()
    top_up(state, Settings(mode="on"), prowlarr, sab, lambda key, r: True, albums.TickResult(), now=lambda: NOW)
    assert record["status"] == albums.GRABBED  # grabbed although the library "has" it
