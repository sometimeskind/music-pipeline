"""Tests for the library-wide album feed (homelab#2403): records with
``origin: "library"``, and where they differ from album playlists' and wants
records."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import music_fetch.albums as albums
from music_fetch import store
from music_fetch.albums import Settings, State, feed_library, feed_wants, held_tracks, origin_counts, tick


@pytest.fixture(autouse=True)
def _no_push():
    with patch.object(albums, "_push"):
        yield


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")


def song(n: int, album_id: str = "A", tracks_count: int = 10) -> dict:
    return {
        "name": f"T{n}", "artists": ["Artist"], "album_id": album_id, "album_name": f"Album {album_id}",
        "album_artist": "Artist", "tracks_count": tracks_count, "album_type": "album", "year": 2020,
        "duration": 200, "song_id": f"{album_id}{n}", "isrc": None, "disc_number": 1, "track_number": n,
        "url": f"https://open.spotify.com/track/{album_id}{n}",
    }


def view(*albums_: tuple, calls: list | None = None):
    """A library hook returning *albums_*: (key, references, tracks, whole, {playlist: songs})."""
    def library():
        if calls is not None:
            calls.append(1)
        return list(albums_)
    return library


A = ("A", 2, 10, False, {"disc": [song(1)], "mood": [song(2), song(1)]})
ON = Settings(mode="on", library="on")


def library_record(status: str = albums.WANTED) -> dict:
    return {"status": status, "blocklist": [], "origin": albums.LIBRARY, "name": "Album A", "artist": "Artist",
            "tracks_count": 10, "playlists": {"disc": [["T1", "Artist", "A1"]]}}


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------


def test_on_queues_an_album_with_every_store_track(caplog) -> None:
    caplog.set_level("INFO")
    state = State()
    feed_library(state, view(A), ON)
    record = state.albums["A"]
    assert record["origin"] == albums.LIBRARY and record["status"] == albums.WANTED
    assert record["references"] == 2 and record["tracks_count"] == 10
    assert record["playlists"] == {"disc": [["T1", "Artist", "A1", None, 1, 1]],
                                   "mood": [["T2", "Artist", "A2", None, 1, 2], ["T1", "Artist", "A1", None, 1, 1]]}
    assert record["duration"] == 2000  # scaled to the whole album for the matcher
    assert "[LIBRARY] 1 album(s) queued (1 new), 1 held off spotdl" in caplog.text


def test_a_backfill_album_takes_the_track_count_from_the_library() -> None:
    state = State()
    feed_library(state, view(("B", 3, 12, False, {"disc": [song(1, "B", tracks_count=0)]})), ON)
    assert state.albums["B"]["tracks_count"] == 12


def test_dry_run_logs_and_queues_nothing(caplog) -> None:
    caplog.set_level("INFO")
    state = State()
    feed_library(state, view(A), Settings(mode="on", library="dry-run"))
    assert state.albums == {}
    assert "[LIBRARY] dry-run: 1 album(s) would be queued (1 new)" in caplog.text
    assert "[LIBRARY] would queue Artist — Album A (A): 2 of 10 track(s) referenced" in caplog.text


@pytest.mark.parametrize("settings", [Settings(mode="on", library="off"), Settings(mode="on", library="on", want_min=0)])
def test_off_reads_nothing_and_drops_the_waiting_records(settings) -> None:
    calls: list = []
    state = State(albums={"A": library_record(), "G": library_record(albums.GRABBED)})
    feed_library(state, view(A, calls=calls), settings)
    assert calls == [] and list(state.albums) == ["G"]


@pytest.mark.parametrize("candidate, why", [
    (("A", 1, 10, False, {"disc": [song(1)]}), None),             # under the threshold
    (("A", 4, 10, True, {"disc": [song(1)]}), None),              # held whole
    (("artist/local", 2, 0, False, {"disc": [song(1)]}), "local"),
    (("A", 2, 10, False, {}), "no playlist track"),               # only unplaylisted items reference it
])
def test_feed_skips(candidate, why, caplog) -> None:
    caplog.set_level("INFO")
    state = State()
    feed_library(state, view(candidate), ON)
    assert state.albums == {}
    if why:
        assert f"; 1 {why} skipped" in caplog.text


def test_feed_leaves_an_album_another_record_has(caplog) -> None:
    caplog.set_level("INFO")
    album = {"status": albums.WANTED, "blocklist": [], "name": "W", "artist": "A", "playlists": {"keep": []}}
    wants = dict(album, origin=albums.WANTS)
    state = State(albums={"A": album, "B": wants})
    feed_library(state, view(A, ("B", 3, 10, False, {"disc": [song(1, "B")]})), ON)
    assert state.albums["A"] is album and state.albums["B"] is wants
    assert "; 1 album playlist skipped; 1 wants skipped" in caplog.text


def test_feed_updates_a_waiting_record_but_not_a_grabbed_one() -> None:
    grabbed = library_record(albums.GRABBED)
    state = State(albums={"A": library_record(albums.MISSING), "B": grabbed})
    feed_library(state, view(A, ("B", 3, 10, False, {"disc": [song(1, "B"), song(2, "B")]})), ON)
    assert set(state.albums["A"]["playlists"]) == {"disc", "mood"}
    assert state.albums["B"] == library_record(albums.GRABBED)


def test_feed_drops_a_waiting_record_that_no_longer_qualifies() -> None:
    state = State(albums={"A": library_record(), "I": library_record(albums.IMPORTED)})
    feed_library(state, view(("A", 1, 10, False, {"disc": [song(1)]})), ON)
    assert list(state.albums) == ["I"]


def test_order_is_album_playlists_then_wants_then_library_most_referenced_first() -> None:
    album = {"status": albums.WANTED, "blocklist": [], "name": "W", "artist": "A", "playlists": {"keep": []}}
    state = State(albums={"W": dict(album, origin=albums.WANTS), "K": album})
    feed_library(state, view(A, ("C", 5, 10, False, {"disc": [song(1, "C")]})), ON)
    assert list(state.albums) == ["K", "W", "C", "A"]


# ---------------------------------------------------------------------------
# With the other feeds
# ---------------------------------------------------------------------------


def test_wants_take_a_waiting_library_record_over() -> None:
    state = State(albums={"A": library_record() | {"references": 3}})
    feed_wants(state, ["disc"], lambda name: [("A", 0, [song(3), song(4)])], Settings(mode="on"))
    record = state.albums["A"]
    assert record["origin"] == albums.WANTS and "references" not in record
    assert [t[2] for t in record["playlists"]["disc"]] == ["A3", "A4"]


def test_wants_leave_a_grabbed_library_record() -> None:
    state = State(albums={"A": library_record(albums.GRABBED)})
    feed_wants(state, ["disc"], lambda name: [("A", 0, [song(3), song(4)])], Settings(mode="on"))
    assert state.albums["A"] == library_record(albums.GRABBED)


def test_an_album_playlist_takes_a_library_record_over() -> None:
    spotify = MagicMock()
    spotify.snapshot_id.return_value = "s1"
    spotify.songs.return_value = [song(1), song(2)]
    state = State(albums={"A": library_record() | {"references": 3}})
    albums.refresh_playlists(state, [("keep", "url")], spotify, albums.TickResult())
    record = state.albums["A"]
    assert "origin" not in record and list(record["playlists"]) == ["keep"]


def test_library_tracks_are_held_after_the_wants_records() -> None:
    wants = {"status": albums.WANTED, "blocklist": [], "origin": albums.WANTS, "name": "W", "artist": "A",
             "playlists": {"disc": [["T", "A", "W1"]]}}
    state = State(albums={"W": wants, "A": library_record()})
    settings = Settings(mode="on", library="on", max_in_flight=1, hold_ahead=1)
    assert held_tracks(state, settings) == {"W1"}
    settings.hold_ahead = 2
    assert held_tracks(state, settings) == {"W1", "A1"}


@pytest.mark.parametrize("status", [albums.MISSING, albums.FAILED])
def test_a_missing_or_failed_library_record_queues_no_fallback(status) -> None:
    state = State(albums={"A": library_record(status)})
    albums.fallbacks(state, lambda playlist, tracks: tracks)
    assert state.albums["A"]["status"] == status


def test_a_library_miss_logs_its_wanted_tracks_staying_with_spotdl() -> None:
    assert albums._back(library_record()) == "; its wanted tracks stay with spotdl"
    assert albums._label(library_record()) == "Artist — Album A (library)"


def test_metrics_carry_the_library_origin() -> None:
    counts = origin_counts(State(albums={"A": library_record()}))
    assert counts[(albums.WANTED, albums.LIBRARY)] == 1 and counts[(albums.IMPORTED, albums.LIBRARY)] == 0


def test_tick_feeds_the_library_after_the_wants(tmp_path: Path) -> None:
    order: list = []
    have = MagicMock(return_value=False)
    prowlarr = MagicMock()
    prowlarr.blocked.return_value = []
    prowlarr.search.return_value = []

    def wants(name):
        order.append("wants")
        return []

    def library():
        order.append("library")
        return [A]

    tick([], MagicMock(), prowlarr, MagicMock(), have, Settings(mode="dry-run", library="on"),
         state_file=tmp_path / "state.json", want_playlists=["disc"], wants=wants, library=library)
    assert order == ["wants", "library"]
    assert State.load(tmp_path / "state.json").albums["A"]["origin"] == albums.LIBRARY


@pytest.mark.parametrize("value, expected", [("on", "on"), ("DRY-RUN", "dry-run"), ("", "off"), ("yes", "off")])
def test_album_library_comes_from_the_environment(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("ALBUM_LIBRARY", value)
    assert Settings.from_env().library == expected
