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
    assert result["B"]["tracks"] == [["One", "Artist"], ["Two", "Artist"]]


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

    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW + timedelta(days=1))
    assert prowlarr.search.call_count == 1
    top_up(state, Settings(mode="on"), prowlarr, sab, never_have, albums.TickResult(), now=lambda: NOW + timedelta(days=8))
    assert prowlarr.search.call_count == 2


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
