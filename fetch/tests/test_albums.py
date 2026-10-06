"""Tests for music_fetch.albums — playlist reduction, budget and the tick's state transitions."""

import json
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


def release(guid: str = "r1", title: str = "Artist-Album-WEB-FLAC-2020") -> Release:
    return Release(guid=guid, title=title, size=60 * MB, indexer_id=1,
                   download_url=f"http://prowlarr/1/download?id={guid}", categories=[3040])


def fakes(releases: list[Release] | None = None):
    prowlarr = MagicMock()
    prowlarr.search.return_value = [release()] if releases is None else releases
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


def run_complete(state, completion, roots, have_result=True, imported=None, tagged=None):
    complete_root, inbox, quarantine = roots
    seen = imported if imported is not None else []
    added = []
    tagged = tagged if tagged is not None else []

    def import_inbox():
        seen.extend(p.relative_to(inbox).as_posix() for p in inbox.rglob("*.flac"))

    status = albums.complete(
        state, completion, import_inbox,
        have=lambda pl, tracks: have_result,
        add_source=lambda have_src, new_src, tracks: added.append((have_src, new_src)),
        tag_ids=lambda pl, tracks, since, count: tagged.append((pl, count)),
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


def test_complete_gives_up_after_max_attempts(roots) -> None:
    state = grabbed_state()
    state.albums["a0"]["attempts"] = albums.MAX_ATTEMPTS - 1
    status, _, _ = run_complete(state, albums.Completion("nzo1", False, "album/Artist-Album-FLAC"), roots)
    assert status == albums.FAILED


def test_complete_ignores_jobs_it_did_not_grab(roots) -> None:
    state = grabbed_state()
    status, seen, _ = run_complete(state, albums.Completion("someone-else", True, "album/Artist-Album-FLAC"), roots)
    assert status is None and seen == []
    assert state.albums["a0"]["status"] == albums.GRABBED


def test_complete_rejects_a_path_outside_the_complete_dir(roots) -> None:
    state = grabbed_state()
    status, seen, _ = run_complete(state, albums.Completion("nzo1", True, "../../etc"), roots)
    assert status == albums.WANTED and seen == []


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


def test_complete_tags_ids_even_when_the_album_is_incomplete(roots) -> None:
    """Tracks that did import keep their IDs; the next release only fills gaps."""
    tagged = []
    run_complete(state := grabbed_state(), albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                 have_result=False, tagged=tagged)
    assert tagged == [("later", 2)]
    assert state.albums["a0"]["status"] == albums.WANTED


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
