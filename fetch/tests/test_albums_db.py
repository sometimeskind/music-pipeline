"""Album mode on the music database (homelab#2410): editions of one release
group as one record, refs for the other edition's Spotify ID, and the tick
skipping while the database is down."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_fetch import albums, events, state, store
from music_fetch.albums import Settings, State, find_albums, plan, requeue, tick

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")


def _song(name: str, album_id: str) -> dict:
    return {"name": name, "artists": ["Artist"], "album_id": album_id, "album_name": "Album",
            "album_artist": "Artist", "tracks_count": 2, "year": 2020,
            "url": f"https://open.spotify.com/track/{album_id}-{name}", "song_id": f"{album_id}-{name}"}


class _Spotify:
    def __init__(self, songs: list[dict]) -> None:
        self._songs = songs

    def snapshot_id(self, url: str) -> str:
        return "s1"

    def songs(self, url: str) -> list[dict]:
        return self._songs

    def album_songs(self, album_id: str) -> list[dict]:
        return []


def _merged() -> State:
    s = State()
    s.albums["sp1"] = {**state.new_record("sp1", status=albums.FALLBACK, blocklist=[], name="Album",
                                          artist="Artist", fallback_from=albums.MISSING),
                       "spotify_album_ids": ["sp1", "sp2"], "release_group": RG, "unresolved": None}
    return s


# --- no database ---


def test_an_override_on_the_other_edition_applies():
    s = _merged()
    assert plan("sp1", s.albums["sp1"], {"sp2": "artist album words"}).queries == ["artist album words"]


def test_the_other_edition_and_the_release_group_find_the_record():
    s = _merged()
    assert [k for k, _ in find_albums(s.albums, "sp2")] == ["sp1"]
    assert [k for k, _ in find_albums(s.albums, RG)] == ["sp1"]
    assert s.find("sp2")[0] == "sp1" and s.find("nope") is None


def test_requeue_by_the_other_edition():
    s = _merged()
    assert [k for k, _ in requeue(s, keys=["sp2"])] == ["sp1"]
    assert s.albums["sp1"]["status"] == albums.WANTED


def test_events_never_take_a_release_group_for_a_spotify_id():
    assert events.spotify_album(RG) is None
    assert events.spotify_album("0ETFjACtuP2ADo6LFhL6HN") == "0ETFjACtuP2ADo6LFhL6HN"


def test_the_tick_skips_while_the_database_is_down(monkeypatch):
    monkeypatch.setenv("MUSIC_DATABASE_URL", "postgresql://nowhere.invalid/music")
    monkeypatch.setattr(state.db, "CONNECT_TIMEOUT", 1)
    spotify = MagicMock()
    with patch.object(albums, "_push") as push:
        result = tick([("later", "url")], spotify, MagicMock(), MagicMock(), lambda k, r: False,
                      Settings(mode="on"))
    assert result.searched == 0 and not spotify.mock_calls
    assert "music_albums_last_tick_success 0" in push.call_args.args[0]


# --- against Postgres ---


@needs_pg
def test_a_tick_maps_and_merges_editions(pg_url, tmp_path: Path):
    with state.connect() as conn:
        rows = state.AlbumRows(albums={
            "sp1": {**state.new_record("sp1", status=albums.IMPORTED, blocklist=[], name="Album", artist="Artist",
                                       playlists={"keep": [["One", "Artist", "sp1-One", None, 1, 1]]}),
                    "release_group": RG, "unresolved": None},
        })
        state.save_albums(conn, rows)
        state.put(conn, state.MB_RELEASES, {"rung": "isrc", "fields": {"mb_releasegroupid": RG}}, key="sp2")
    prowlarr = MagicMock()
    prowlarr.blocked.return_value = []
    with patch.object(albums, "_push"):
        tick([("later", "url")], _Spotify([_song("One", "sp2"), _song("Two", "sp2")]), prowlarr, MagicMock(),
             lambda k, r: False, Settings(mode="dry-run"), library=None, wants=None)
    loaded = State.load()
    assert list(loaded.albums) == ["sp1"]
    record = loaded.albums["sp1"]
    assert record["spotify_album_ids"] == ["sp1", "sp2"]
    assert record["status"] == albums.IMPORTED
    assert [t[0] for t in record["playlists"]["later"]] == ["One", "Two"]
    assert [t[0] for t in record["playlists"]["keep"]] == ["One"]
    assert store.load("later").songs[0]["album_id"] == "sp2"
    # The next refresh finds sp2's songs on the merged record through its refs.
    loaded.playlists["later"]["snapshot_id"] = "old"
    with patch.object(albums, "_push"):
        loaded.save()
        tick([("later", "url")], _Spotify([_song("Three", "sp2")]), prowlarr, MagicMock(),
             lambda k, r: False, Settings(mode="dry-run"))
    again = State.load()
    assert list(again.albums) == ["sp1"]
    assert [t[0] for t in again.albums["sp1"]["playlists"]["later"]] == ["Three"]
    assert "keep" in again.albums["sp1"]["playlists"]
